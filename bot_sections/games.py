"""Game state, game handlers, results, game routing helpers and board games."""

class GameSection:
    def broadcast_game_rooms(self, text: str, first_room: str = ""):
        """Quickly announce a game, delivering to its initiating room first."""
        rooms = [room for room in self._active_rooms() if not _game_results_disabled_for_room(room)]
        first_room = str(first_room or "").strip()
        if first_room and not _game_results_disabled_for_room(first_room):
            rooms = [first_room] + [
                room for room in rooms if _norm_room(room) != _norm_room(first_room)
            ]
        lock = getattr(self, "_game_broadcast_lock", None)
        if lock is None:
            lock = self._game_broadcast_lock = threading.Lock()
        sent = 0
        with lock:
            for index, target_room in enumerate(rooms):
                try:
                    self.send_room_text(target_room, text)
                    sent += 1
                except Exception as exc:
                    self.log("[GAME-BROADCAST] failed", target_room, repr(exc))
                if index + 1 < len(rooms):
                    # Text-only game announcements are much smaller than media
                    # bursts; keep a tiny pacing gap without multi-second waits.
                    time.sleep(0.05)
        return sent

    def broadcast_game_rooms_async(self, text: str, first_room: str = ""):
        """Queue a broad game announcement without blocking command delivery."""
        worker = threading.Thread(
            target=self.broadcast_game_rooms,
            args=(text, first_room),
            daemon=True,
            name="game-room-announcement",
        )
        worker.start()
        return True

    def _send_game_open_announcement(self, room, text):
        """Send one opening notice per room and suppress back-to-back notices.

        Royal arrow/diamond and ordinary-shot schedulers can wake at nearly
        the same time, or be started twice during reconnect recovery. Keep a
        short per-room gate so users never receive duplicate opening notices
        one after another. The event itself remains valid; only the repeated
        notification is suppressed.
        """
        room = str(room or "").strip()
        if not room:
            return False
        try:
            gap = max(0.0, float(os.getenv("GAME_OPEN_ANNOUNCEMENT_GAP_SECONDS", "8") or 8))
        except (TypeError, ValueError):
            gap = 8.0
        lock = getattr(self, "_game_open_announcement_lock", None)
        if lock is None:
            self._game_open_announcement_lock = lock = threading.RLock()
        now = time.monotonic()
        key = _norm_room(room)
        with lock:
            recent = getattr(self, "_game_open_announcement_recent", None)
            if not isinstance(recent, dict):
                recent = self._game_open_announcement_recent = {}
            previous = recent.get(key)
            if isinstance(previous, dict) and now - float(previous.get("at", 0) or 0) < gap:
                self.log("[GAME-ANNOUNCE] suppressed back-to-back opening notice:", room)
                return False
            recent[key] = {"at": now, "text": str(text or "")[:240]}
            if len(recent) > 512:
                cutoff = now - max(60.0, gap * 10.0)
                self._game_open_announcement_recent = {
                    saved_key: saved for saved_key, saved in recent.items()
                    if isinstance(saved, dict) and float(saved.get("at", 0) or 0) >= cutoff
                }
        try:
            return self.send_room_text(room, text) is not False
        except Exception:
            with lock:
                current = getattr(self, "_game_open_announcement_recent", {}).get(key)
                if isinstance(current, dict) and current.get("at") == now:
                    getattr(self, "_game_open_announcement_recent", {}).pop(key, None)
            raise

    def _board_game_ready(self, username, room, game_type="board"):
        """Enforce a separate 4-minute interval; env-configured masters bypass it."""
        if not username or _is_env_master_for_game_cooldown(username):
            return True, 0
        key = ("board_games", str(game_type or "board").casefold(), _norm_user(username))
        now = time.time()
        with self.game_lock:
            last = getattr(self, "board_game_cooldown", {}).get(key, 0.0)
            if now - last < self.BOARD_GAME_COOLDOWN:
                return False, int(self.BOARD_GAME_COOLDOWN - (now - last)) + 1
            self.board_game_cooldown[key] = now
        return True, 0

    def _board_game_cooldown_notice(self, room, username, game_type="board"):
        ok, left = self._board_game_ready(username, room, game_type)
        if not ok:
            mins = left // 60
            secs = left % 60
            wait = f"{mins} دقيقة و{secs} ثانية" if mins else f"{secs} ثانية"
            game_label = "السلم والثعبان" if str(game_type).casefold() == "snake" else "لودو"
            self.send_room_text(room, f"⏳ @{username} انتظر {wait} قبل بدء لعبة {game_label}.\n🎮 الفاصل بين لعبتي {game_label} لنفس اللاعب هو 4 دقائق.")
        return ok

    def _game_award(self, username, amount):
        # The common reward path atomically diverts 20% to an outstanding loan.
        if not username:
            return 0
        result = _add_points(username, int(amount), repay_loan=True, return_details=True)
        self._notify_loan_repayment(username, result)
        return int(result.get("balance", 0) or 0) if isinstance(result, dict) else int(result or 0)

    def _game_ready(self, username, room, cooldown=40.0, game_name=""):
        # الفاصل مستقل لكل لعبة ولكل مستخدم. لا نفرض 40 ثانية هنا،
        # بل نستخدم المدة التي يحددها مستدعي اللعبة.
        # المليون = 60 ثانية، المليار = 600 ثانية (10 دقائق).
        game_key = _norm_user(str(game_name or "general").replace("ة", "ه")) or "general"
        try:
            cooldown = max(0.0, float(cooldown))
        except (TypeError, ValueError):
            cooldown = 40.0
        # Only deployment-configured master accounts bypass all per-game cooldowns.
        if _is_env_master_for_game_cooldown(username):
            return True, 0
        key = (game_key, _norm_user(username))
        now = time.time()
        with self.game_lock:
            last = self.game_cooldown.get(key, 0.0)
            if now - last < cooldown:
                return False, int(cooldown - (now - last)) + 1
            self.game_cooldown[key] = now
        return True, 0

    def _game_cooldown_notice(self, room, username, cooldown=40.0, game_name="", private_to=""):
        # مرر مدة اللعبة كما هي؛ لا تستبدلها افتراضياً بـ40 ثانية.
        try:
            cooldown = max(0.0, float(cooldown))
        except (TypeError, ValueError):
            cooldown = 40.0
        ok, left = self._game_ready(username, room, cooldown, game_name)
        if not ok:
            label = str(game_name or "اللعبة").strip() or "اللعبة"
            if cooldown >= 60 and cooldown % 60 == 0:
                minutes = int(cooldown // 60)
                interval = f"{minutes} دقائق" if minutes != 1 else "دقيقة واحدة"
            else:
                interval = f"{int(cooldown)} ثانية"
            message = (
                f"⏳ @{username} انتظر {left} ثانية قبل إعادة لعبة {label}.\n"
                f"🎮 الفاصل {interval} لنفس اللعبة فقط، ويمكنك لعب لعبة أخرى الآن."
            )
            if private_to:
                self.send_private_text(private_to, message)
            else:
                self.send_room_text(room, message)
        return ok

    def _send_game_result(self, room, text, game_key, winner_name="", target_rooms=None):
        """Send the text result and, when the game has artwork, its winner card."""
        self.send_room_text(room, text)
        if winner_name and game_key:
            self._send_game_winner_card(
                game_key, winner_name, target_rooms or [room],
                respect_room_filter=False,
            )

    def _send_game_winner_card(self, game_key, winner_name, target_rooms, send_delay=0.0,
                               respect_room_filter=True):
        """Render the existing game artwork and send it safely room-by-room.

        ``send_delay`` is used by multi-room game broadcasts to avoid a burst
        of media packets that can trigger the TalkinChat server to close the
        WebSocket. Existing callers keep the original zero-delay behaviour.
        """
        target_rooms = [
            str(room or "").strip() for room in (target_rooms or [])
            if str(room or "").strip()
            and (not respect_room_filter or not _game_results_disabled_for_room(room))
        ]
        if not target_rooms:
            return False
        base = _public_base_url()
        if not base:
            self.log("[GAME] winner card skipped: public media URL is not configured")
            return False
        filename = GAME_IMAGE_FILES.get(game_key)
        image = ASSETS_DIR / filename if filename else None
        if not image or not image.is_file():
            self.log("[GAME] winner card skipped: no artwork", game_key)
            return False
        try:
            if game_key == "اسرق_فشل":
                # The failure artwork already contains its own result design.
                # Sending it through render_game_winner_card() incorrectly
                # stamped a gold WINNER/name overlay onto the failure image.
                url = self._game_public_image(image, route="assets")
                if not url:
                    self.log("[GAME] failure artwork URL unavailable", game_key)
                    return False
            else:
                winner_key = _norm_user(winner_name)
                photo_cache = getattr(self, "user_photos", {})
                winner_photo = ""
                if isinstance(photo_cache, dict):
                    winner_photo = (photo_cache.get(winner_key, "")
                                    or photo_cache.get(str(winner_name or "").casefold(), ""))
                if not winner_photo:
                    winner_photo = self._lookup_profile_photo(winner_name)
                language = self._effective_room_language(target_rooms[0]) if game_key == "hunt_dragon" and target_rooms else "ar"
                card = render_game_winner_card(game_key, winner_name, winner_photo, language=language)
                url = f"{base}/games/{card.name}"
            self._verify_public_media_url(url, "image")
            rooms = []
            seen = set()
            for r in (target_rooms or []):
                r = str(r or "").strip()
                k = r.casefold()
                if r and k not in seen:
                    seen.add(k); rooms.append(r)
            for index, r in enumerate(rooms):
                # Send sequentially. A short pause is important for an image
                # broadcast: the server may close the realtime socket when
                # several room-media packets arrive back-to-back.
                try:
                    self.send_room_media(r, url, "image")
                except Exception as exc:
                    self.log("[GAME] winner card room send failed:", r, repr(exc))
                if send_delay > 0 and index + 1 < len(rooms):
                    time.sleep(float(send_delay))
            self.log("[GAME] winner card sent", game_key, winner_name, len(rooms))
            return bool(rooms)
        except Exception as exc:
            self.log("[GAME] winner card send failed:", game_key, repr(exc))
            return False

    def game_help(self, room, sender=None):
        """Open the canonical A3 game catalog and let ns page through it."""
        if sender:
            key = (str(room or ""), _norm_user(sender))
            self.help_pages[key] = 3
            self.help_page_part[key] = 1
            self.help_game_part[key] = 1
        return self._send_game_help_section(room=room, part=1)

    def _game_balance_ok(self, username, amount):
        return _get_points(username) >= int(amount)

    def _load_wager_state(self):
        """Restore open wagers whose stakes were already reserved."""
        data = _load_local_json(WAGER_STATE_FILE, {})
        rows = data.get("waiting", data) if isinstance(data, dict) else {}
        if not isinstance(rows, dict):
            return {}
        restored = {}
        for key, value in rows.items():
            if not isinstance(value, dict) or not value.get("user"):
                continue
            try:
                stake = int(value.get("stake", 0) or 0)
                created = float(value.get("created", 0) or 0)
            except (TypeError, ValueError):
                continue
            if stake <= 0 or created <= 0:
                continue
            item = dict(value)
            item.update(stake=stake, created=created, reserved=bool(value.get("reserved", True)))
            item["id"] = str(item.get("id") or uuid.uuid4().hex)
            restored[str(key).casefold()] = item
        return restored

    def _recover_pending_wager_debits(self):
        """Finish a wager debit interrupted by a process/platform restart."""
        recovered = []
        with self.game_lock:
            rows = [
                (key, dict(value)) for key, value in self.wager_waiting.items()
                if isinstance(value, dict) and value.get("status") == "pending_debit"
            ]
        for key, waiting in rows:
            user = str(waiting.get("user") or "").strip()
            stake = int(waiting.get("stake", 0) or 0)
            challenge_id = str(waiting.get("id") or "")
            txid = str(waiting.get("debit_transaction_id") or f"wager-debit:{challenge_id}")
            if not user or stake <= 0 or not challenge_id:
                continue
            debit = _apply_points_delta_once(
                user, -stake, txid, f"reserve {waiting.get('game', 'wager')} stake"
            )
            if not debit.get("ok"):
                if debit.get("error") == "insufficient":
                    with self.game_lock:
                        current = self.wager_waiting.get(key)
                        if current and str(current.get("id") or "") == challenge_id:
                            self.wager_waiting.pop(key, None)
                            self._save_wager_state()
                continue
            with self.game_lock:
                current = self.wager_waiting.get(key)
                if current and str(current.get("id") or "") == challenge_id:
                    current["status"] = "active"
                    current["reserved"] = True
                    current["debit_transaction_id"] = txid
                    recovered.append(key)
                    self._save_wager_state()
        if recovered:
            self.log("[GAME] recovered wager reservations after restart", len(recovered))

    def _save_wager_state(self):
        """Persist open wagers through the shared atomic JSON writer."""
        with self.game_lock:
            snapshot = {
                str(key): dict(value)
                for key, value in self.wager_waiting.items()
                if isinstance(value, dict)
            }
        _save_local_json(WAGER_STATE_FILE, {"version": 1, "waiting": snapshot})

    def _cleanup_expired_wagers(self):
        # مدة البحث عن منافس لأي لعبة انتظار = دقيقتان فقط.
        timeout = 120
        now = time.time()
        expired = []
        with self.game_lock:
            for key, waiting in list(self.wager_waiting.items()):
                try:
                    is_expired = now - float(waiting.get("created", 0) or 0) >= timeout
                except (TypeError, ValueError):
                    is_expired = True
                if is_expired and not waiting.get("_refund_pending"):
                    user = str(waiting.get("user") or "")
                    created = str(waiting.get("created", 0) or 0)
                    challenge_id = str(waiting.get("id") or "")
                    if not challenge_id:
                        challenge_id = uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"wager-timeout:{key}:{_norm_user(user)}:{created}",
                        ).hex
                        waiting["id"] = challenge_id
                    waiting["_refund_pending"] = True
                    expired.append((key, dict(waiting), challenge_id))
            if expired:
                self._save_wager_state()
        for key, waiting, challenge_id in expired:
            room = str(waiting.get("room") or "").strip()
            game = str(waiting.get("game") or "اللعبة").strip()
            user = str(waiting.get("user") or "").strip()
            stake = max(0, int(waiting.get("stake", 0) or 0))
            reserved = bool(waiting.get("reserved"))
            if reserved:
                refund = _apply_points_delta_once(
                    user, stake, f"wager-timeout-refund:{challenge_id}",
                    f"refund expired {game} wager",
                ) if user and stake > 0 else {"ok": False, "error": "invalid_wager"}
                if not refund.get("ok"):
                    with self.game_lock:
                        current = self.wager_waiting.get(key)
                        if current and str(current.get("id") or "") == challenge_id:
                            current.pop("_refund_pending", None)
                    self.log("[GAME] expired wager refund failed; will retry", game, user, refund.get("error"))
                    continue
            removed = False
            with self.game_lock:
                current = self.wager_waiting.get(key)
                if current and str(current.get("id") or "") == challenge_id:
                    self.wager_waiting.pop(key, None)
                    removed = True
                    self._save_wager_state()
            if not removed:
                continue
            if room:
                english = self._effective_room_language(room) == "en"
                if reserved:
                    text = (
                        f"⌛ The {game} wager expired without an opponent; "
                        f"{_fmt_points(stake)} points were refunded to @{user}."
                        if english else
                        f"⌛ انتهى وقت انتظار منافس للعبة {game}؛ أُعيد مبلغ "
                        f"{_fmt_points(stake)} نقطة إلى @{user}."
                    )
                else:
                    text = f"⌛ The {game} wager expired without an opponent." if english else f"⌛ انتهى وقت انتظار منافس للعبة {game}."
                try:
                    self.send_room_text(room, text)
                except Exception as exc:
                    self.log("[GAME] expired wager notice failed:", room, repr(exc))
            self.log("[GAME] expired wager refunded", game, user, stake if reserved else 0)

    def _retry_pending_wager_refund(self, key, waiting):
        """Finish a timeout refund left pending by a concurrent cleanup worker."""
        if not isinstance(waiting, dict) or not waiting.get("_refund_pending"):
            return True
        user = str(waiting.get("user") or "").strip()
        stake = max(0, int(waiting.get("stake", 0) or 0))
        challenge_id = str(waiting.get("id") or "")
        if not challenge_id:
            return False
        refund = _apply_points_delta_once(
            user, stake, f"wager-timeout-refund:{challenge_id}",
            f"retry refund expired {waiting.get('game', 'wager')} wager",
        ) if user and stake > 0 and waiting.get("reserved") else {"ok": True}
        if not refund.get("ok"):
            return False
        with self.game_lock:
            current = self.wager_waiting.get(key)
            if current and str(current.get("id") or "") == challenge_id:
                self.wager_waiting.pop(key, None)
                self._save_wager_state()
        return True

    def _cleanup_expired_fixed_games(self):
        # ألعاب سنارة/برق/ياقوت/صدام/كاشف تنتظر منافساً لمدة دقيقتين فقط.
        timeout = 120
        now = time.time()
        expired = []
        with self.game_lock:
            for game_name, waiting in list(self.fixed_game_waiting.items()):
                if now - float(waiting.get("created", 0) or 0) >= timeout:
                    expired.append((game_name, waiting))
                    self.fixed_game_waiting.pop(game_name, None)
        for game_name, waiting in expired:
            room = str(waiting.get("room") or "").strip()
            if waiting.get("reserved"):
                _add_points(waiting.get("user"), int(waiting.get("prize", 0) or 0))
            if room:
                self.send_room_text(room, f"✅ انتهت لعبة {game_name}")
            self.log("[GAME] expired fixed game", game_name, waiting.get("user"))

    def _wager_result(self, first, second, game_name):
        """Resolve a global PvP wager, honoring one admin-configured 3:1 sequence."""
        first_key = _norm_user(first.get("user"))
        second_key = _norm_user(second.get("user"))
        with _WAGER_ADMIN_RULES_LOCK:
            rules = _load_local_json(WAGER_ADMIN_RULES_FILE, {})
            if not isinstance(rules, dict): rules = {}
            active = [
                (first_key, first) for _ in (0,)
                if isinstance(rules.get(first_key), dict) and rules[first_key].get("enabled")
            ] + [
                (second_key, second) for _ in (0,)
                if isinstance(rules.get(second_key), dict) and rules[second_key].get("enabled")
            ]
            if len(active) == 1:
                target_key, target_player = active[0]
                rule = rules[target_key]
                cycle = int(rule.get("cycle", 0) or 0) % 4
                target_wins = cycle < 3
                opponent = second if target_player is first else first
                winner, loser = (target_player, opponent) if target_wins else (opponent, target_player)
                rule["cycle"] = (cycle + 1) % 4
                rule["wins"] = int(rule.get("wins", 0) or 0) + (1 if target_wins else 0)
                rule["losses"] = int(rule.get("losses", 0) or 0) + (0 if target_wins else 1)
                rule["last_result"] = "win" if target_wins else "loss"
                rule["updated_at"] = int(time.time())
                _save_local_json(WAGER_ADMIN_RULES_FILE, rules)
                self.log("[WAGER_ADMIN_RULE]", target_key, "result=", rule["last_result"], "cycle=", rule["cycle"])
            else:
                # No configured target, or two managed targets playing each
                # other: keep the normal random resolution rather than favoring
                # one side arbitrarily.
                winner, loser = (first, second) if secrets.randbelow(2) == 0 else (second, first)
        winner_stake = int(winner.get("stake", 0) or 0)
        loser_stake = int(loser.get("stake", 0) or 0)

        # Both players enter at the first player's shared stake. Both stakes
        # were reserved atomically when they joined. Do not debit again here:
        # return the winner's stake and add the loser's stake to the winner.
        award = _add_points(
            winner.get("user"), winner_stake + loser_stake,
            repay_loan=True, loan_repayment_basis=loser_stake, return_details=True,
        )
        self._notify_loan_repayment(winner.get("user"), award)

        game_key = {
            "رهان":"bet", "مراهنة":"bet", "مضاربة":"duel", "مضاربه":"duel",
            "استثمار":"investment", "حظي":"luck"
        }.get(game_name, game_name.casefold())
        _record_game(loser.get("user"), game_key, -loser_stake, loser_stake)
        _record_game(winner.get("user"), game_key, loser_stake, loser_stake)

        winner_name = str(winner.get("user", ""))
        loser_name = str(loser.get("user", ""))
        winner_profit = loser_stake
        loser_loss = loser_stake
        text = (
            f"🏆 انتهت لعبة {game_name}\n"
            f"👑 الفائز: @{winner_name}\n"
            f"💰 مبلغ الفوز: +{_fmt_points(winner_profit)} نقطة\n"
            f"❌ الخاسر: @{loser_name}\n"
            f"💸 مبلغ الخسارة: -{_fmt_points(loser_loss)} نقطة"
        )

        # IMPORTANT: the queue is global across rooms, but the result is local
        # to exactly the two rooms where the two players entered the game.
        result_rooms = []
        seen_rooms = set()
        for target in (first.get("room"), second.get("room")):
            target = str(target or "").strip()
            key = target.casefold()
            if target and key not in seen_rooms:
                seen_rooms.add(key)
                result_rooms.append(target)

        for result_room in result_rooms:
            self.send_room_text(result_room, text)

        self._send_game_winner_card(game_key, str(winner.get("user") or ""), result_rooms)

    def _queue_wager(self, room, sender, game_name, amount):
        try:
            amount = int(amount)
        except Exception:
            return True
        if amount <= 0:
            self.send_room_text(room, _reply_template("game_invalid_amount", DEFAULT_REPLY_MESSAGES["game_invalid_amount"]))
            return True
        if amount < WAGER_MIN_AMOUNT:
            message = (
                "❌ الحد الأدنى للرهان هو 100k نقطة. اكتب 100k أو مبلغًا أكبر."
                if self._effective_room_language(room) != "en"
                else "❌ The minimum wager is 100k points. Enter 100k or more."
            )
            self.send_room_text(room, message)
            return True

        def insufficient_notice(balance, required, reserved_points=0, joining=False):
            balance = max(0, int(balance or 0))
            required = max(0, int(required or 0))
            reserved_points = max(0, int(reserved_points or 0))
            available = max(0, balance - reserved_points)
            if self._effective_room_language(room) == "en":
                note = (
                    f"\nYour command amount does not lower the first player's stake ({_fmt_points(required)})."
                    if joining else ""
                )
                return (
                    f"❌ Insufficient available balance. Balance: {_fmt_points(balance)}; "
                    f"available now: {_fmt_points(available)}; required: {_fmt_points(required)}."
                    + (f" Pending debit: {_fmt_points(reserved_points)}." if reserved_points else "") + note
                )
            note = (
                f"\n📌 مبلغك المكتوب لا يخفض رهان اللاعب الأول ({_fmt_points(required)})."
                if joining else ""
            )
            return (
                f"❌ رصيدك المتاح لا يكفي. رصيدك الحالي: {_fmt_points(balance)}، "
                f"المتاح الآن: {_fmt_points(available)}، والمطلوب: {_fmt_points(required)}."
                + (f"\n⏳ يوجد خصم رهان لم يكتمل بعد بقيمة {_fmt_points(reserved_points)}." if reserved_points else "")
                + note
            )
        # أولاً ننظف الألعاب التي تجاوزت دقيقتين، ثم نتحقق هل لدى المستخدم
        # نفس اللعبة معلقة بالفعل. هذا الفحص يسبق فاصل الـ40 ثانية حتى يظهر
        # تنبيه واضح بدلاً من أن يصمت البوت أو يعطي رسالة فاصل عادية.
        self._cleanup_expired_wagers()
        with self.game_lock:
            existing = self.wager_waiting.get(game_name.casefold())
            if existing and existing.get("_refund_pending"):
                pending_refund = dict(existing)
            else:
                pending_refund = None
        if pending_refund is not None:
            if self._retry_pending_wager_refund(game_name.casefold(), pending_refund):
                existing = None
            else:
                self.send_room_text(
                    room,
                    "⌛ انتهت مهلة هذا الرهان ويجري إعادة مبلغه؛ جرّب بعد لحظات."
                    if self._effective_room_language(room) != "en"
                    else "⌛ This wager expired and its refund is processing; try again shortly.",
                )
                return True
        with self.game_lock:
            existing = self.wager_waiting.get(game_name.casefold())
            if existing and _norm_user(existing.get("user")) == _norm_user(sender):
                self.send_room_text(room, f"⏳ @{sender} لديك لعبة {game_name} شغالة حالياً.\n🎯 ما زالت تبحث عن منافس.\n⌛ مدة البحث دقيقتان فقط، وبعدها تُلغى تلقائياً إذا لم يدخل منافس.")
                return True

        # Keep aliases of the same wager on one two-minute per-player cooldown.
        cooldown_game = {
            "رهان": "bet", "مراهنة": "bet", "مراهنه": "bet",
            "مضاربة": "duel", "مضاربه": "duel",
            "استثمار": "investment", "حظي": "luck",
            "حظ يا نصيب": "luck investment",
        }.get(str(game_name).casefold(), str(game_name))
        if not self._game_cooldown_notice(room, sender, WAGER_GAME_COOLDOWN_SECONDS, cooldown_game):
            return True

        # GLOBAL queue: the same game challenge is shared by every room.
        # Same-room play is allowed; room is intentionally NOT part of the key.
        key = game_name.casefold()
        waiting = None
        error = None
        with self.game_lock:
            waiting = self.wager_waiting.get(key)
            if waiting and waiting.get("_refund_pending"):
                error = (
                    "⌛ انتهت مهلة هذا الرهان ويجري إعادة مبلغه؛ جرّب بعد لحظات."
                    if self._effective_room_language(room) != "en"
                    else "⌛ This wager expired and its refund is processing; try again shortly."
                )
            elif waiting and _norm_user(waiting["user"]) == _norm_user(sender):
                return True

            # A player cannot spend points that are already committed to another
            # open global challenge. The current challenge itself is excluded.
            # Active challenges already had their stake deducted from the
            # balance. Counting them again here double-reserved the same points
            # and caused false "insufficient balance" errors. Only an interrupted
            # pending_debit still needs to be held against concurrent wagers.
            reserved = sum(
                int(w.get("stake", 0) or 0) for k, w in self.wager_waiting.items()
                if k != key and _norm_user(w.get("user")) == _norm_user(sender)
                and str(w.get("status") or "active").casefold() == "pending_debit"
            )
            # The first player sets the stake for the whole round. A later
            # player's amount in the command does not change that stake: they
            # must have enough balance for the original challenge amount.
            round_stake = int(waiting.get("stake", 0) or 0) if waiting else amount
            balance = _get_points(sender)
            if error is None and waiting and round_stake < WAGER_MIN_AMOUNT:
                error = (
                    "❌ هذا الرهان المعلّق أقل من الحد الأدنى الجديد (100k)؛ لن أقبل الانضمام إليه، "
                    "وسينتهي تلقائيًا ثم يُعاد مبلغ صاحبه."
                    if self._effective_room_language(room) != "en"
                    else "❌ This open wager is below the 100k minimum and cannot be joined. It will expire and be refunded."
                )
            if error is None and balance < round_stake + reserved:
                    error = insufficient_notice(balance, round_stake, reserved, joining=bool(waiting))

            if error is None and waiting:
                # A second player may enter ANY amount, from the same room or another room.
                # Their room and stake are both preserved for the final result.
                # Keep the first challenge persisted until the second player's
                # debit is idempotently committed below; a restart between
                # these operations must never lose either the challenge or the
                # second player's points.
                pass
            elif error is None:
                # Persist the reservation before debiting. If the platform
                # stops between these two operations, startup resumes the
                # pending debit with the same idempotent transaction id.
                challenge_id = uuid.uuid4().hex
                self.wager_waiting[key] = {
                    "id": challenge_id,
                    "user": sender,
                    "room": room,
                    "stake": amount,
                    "reserved": True,
                    "status": "pending_debit",
                    "debit_transaction_id": f"wager-debit:{challenge_id}",
                    "game": game_name,
                    "created": time.time(),
                }
                self._save_wager_state()
                debit = _apply_points_delta_once(
                    sender, -amount, f"wager-debit:{challenge_id}",
                    f"reserve {game_name} stake",
                )
                if not debit.get("ok"):
                    self.wager_waiting.pop(key, None)
                    self._save_wager_state()
                    error = insufficient_notice(
                        _get_points(sender), amount, reserved,
                    ) if debit.get("error") == "insufficient" else "❌ تعذر حفظ حجز الرهان بأمان."
                else:
                    self.wager_waiting[key]["status"] = "active"
                    self._save_wager_state()

        if error:
            self.send_room_text(room, error)
            return True

        if waiting:
            # Mirror the first player's wager exactly; do not let a smaller
            # amount supplied by the challenger understate the loser debit.
            round_stake = int(waiting.get("stake", 0) or 0)
            challenge_id = str(waiting.get("id") or key)
            join_txid = f"wager-join-debit:{challenge_id}:{_norm_user(sender)}"
            debit = _apply_points_delta_once(
                sender, -round_stake, join_txid,
                f"join {game_name} wager",
            )
            if not debit.get("ok"):
                self.send_room_text(
                    room,
                    insufficient_notice(
                        _get_points(sender), round_stake,
                        joining=True,
                    ) if debit.get("error") == "insufficient" else
                    "❌ تعذر تثبيت مبلغ الرهان بأمان؛ لم تُقبل المشاركة."
                )
                return True
            with self.game_lock:
                current = self.wager_waiting.get(key)
                if current and str(current.get("id") or "") == challenge_id:
                    self.wager_waiting.pop(key, None)
                    self._save_wager_state()
            second = {
                "user": sender,
                "room": room,
                "stake": round_stake,
                "reserved": True,
                "game": game_name,
            }
            self._wager_result(waiting, second, game_name)

            # _game_ready already recorded the 30-second cooldown per game/user.
            # It is independent of room, so changing rooms or using another game
            # does not create a false cooldown.
            return True

        # Opening message is explicit per game so the user-facing wording
        # cannot fall back to an old generic message. The participation line
        # always shows the placeholder "المبلغ" rather than the actual stake.
        game_labels = {
            "رهان": ("رهان", "راهن", "رهان"),
            "مراهنة": ("رهان", "راهن", "رهان"),
            "مراهنه": ("رهان", "راهن", "رهان"),
            "مضاربة": ("مضاربة", "ضارب", "مضاربة"),
            "مضاربه": ("مضاربة", "ضارب", "مضاربة"),
            "استثمار": ("استثمار", "استثمر", "استثمار"),
            "حظي": ("حظي", "راهن", "حظي"),
        }
        game_label, verb, command = game_labels.get(game_name, (game_name, "لاعب", game_name))
        opening = (
            f"🎮 بدأت لعبة {game_label}\n"
            f"👤 @{sender}\n"
            f"💰 المبلغ: {_fmt_points(amount)} نقطة\n"
            f"🎯 اكتب {command}@المبلغ لبدء الرهان"
        )
        # GLOBAL challenge announcement: every tracked bot room sees the same
        # open challenge, regardless of where the first player started.
        self.broadcast_game_rooms(opening, first_room=room)
        return True

    def _fixed_game_result(self, first, second, game_name, prize=500):
        """Resolve a global fixed-prize game. Winner +prize, loser -prize.
        The challenge is global, while the final text/image is sent only to the
        two rooms where the two players entered.
        """
        prize = int(prize)
        winner, loser = (first, second) if secrets.randbelow(2) == 0 else (second, first)

        # Both stakes were reserved when players joined. Return the winner's
        # stake plus the loser's stake; never debit again at settlement.
        award = _add_points(
            winner.get("user"), prize * 2,
            repay_loan=True, loan_repayment_basis=prize, return_details=True,
        )
        self._notify_loan_repayment(winner.get("user"), award)

        game_key=game_name
        _record_game(loser.get("user"), game_key, -prize, prize)
        _record_game(winner.get("user"), game_key, prize, prize)

        winner_name = str(winner.get("user", ""))
        loser_name = str(loser.get("user", ""))
        text=(
            f"🏆 انتهت لعبة {game_name}\n"
            f"👑 الفائز: @{winner_name}\n"
            f"💰 مبلغ الفوز: +{_fmt_points(prize * 2)} نقطة\n"
            f"❌ الخاسر: @{loser_name}\n"
            f"💸 مبلغ الخسارة: -{_fmt_points(prize)} نقطة"
        )

        result_rooms=[]
        seen=set()
        for target in (first.get("room"), second.get("room")):
            target=str(target or "").strip()
            key=target.casefold()
            if target and key not in seen:
                seen.add(key); result_rooms.append(target)
        for result_room in result_rooms:
            self.send_room_text(result_room,text)

        self._send_game_winner_card(game_key, str(winner.get("user") or ""), result_rooms)

    def _queue_fixed_game(self, room, sender, game_name, prize=500):
        """Global two-player queue; both players may enter from the same room or different rooms."""
        prize=int(prize)
        self._cleanup_expired_fixed_games()
        with self.game_lock:
            existing = self.fixed_game_waiting.get(game_name)
            if existing and _norm_user(existing.get("user")) == _norm_user(sender):
                self.send_room_text(room, f"⏳ @{sender} لديك لعبة {game_name} شغالة حالياً.\n🎯 ما زالت تبحث عن منافس.\n⌛ مدة البحث دقيقتان فقط، وبعدها تُلغى تلقائياً إذا لم يدخل منافس.")
                return True

        if not self._game_cooldown_notice(room, sender, WAGER_GAME_COOLDOWN_SECONDS, game_name):
            return True
        if _get_points(sender) < prize:
            self.send_room_text(room,f"❌ تحتاج {prize} نقطة للمشاركة في {game_name}. رصيدك: {_fmt_points(_get_points(sender))}")
            return True

        with self.game_lock:
            waiting=self.fixed_game_waiting.get(game_name)
            if waiting and _norm_user(waiting.get("user")) == _norm_user(sender):
                return True
            if waiting:
                self.fixed_game_waiting.pop(game_name,None)

        if waiting:
            _add_points(sender, -prize)
            second={"user":sender,"room":room,"reserved":True,"prize":prize}
            self._fixed_game_result(waiting,second,game_name,prize)
            # Cooldown was already recorded by _game_ready using only game + user.
            # Same-room participation remains allowed.
            return True

        challenge=(
            f"🎮 بدأت لعبة {game_name}\n"
            f"👤 @{sender}\n"
            f"💰 مبلغ الفوز: {_fmt_points(prize * 2)} نقطة\n"
            f"🎯 اكتب {game_name} للمنافسة"
        )
        with self.game_lock:
            # Re-check in case another event created the queue while we prepared.
            if game_name not in self.fixed_game_waiting:
                _add_points(sender, -prize)
                self.fixed_game_waiting[game_name]={"user":sender,"room":room,"created":time.time(),"reserved":True,"prize":prize}
            else:
                return True
        # إعلان بداية الألعاب الخمس يكون في الغرفة التي بدأت منها اللعبة فقط.
        # لا نرسل رسالة البحث عن منافس إلى جميع الغرف.
        self.send_room_text(room, challenge)
        return True

    def _farm_check_command(self, sender, english=False):
        if not BOT_MASTER or _norm_user(sender) != _norm_user(BOT_MASTER):
            self.send_private_text(
                sender,
                "🔒 أمر فحص زرع مخصص للماستر الأساسي فقط."
                if not english else "🔒 Farm check is reserved for the primary master only.",
            )
            return True
        crops = list(self._crop_options().items())
        if not hasattr(self, "_farm_check_lock"):
            self._farm_check_lock = threading.Lock()
        with self._farm_check_lock:
            index = int(getattr(self, "_farm_check_index", 0) or 0)
            if index < 0 or index >= len(crops):
                index = 0
            crop, (minutes, reward) = crops[index]
            self._farm_check_index = (index + 1) % len(crops)
        try:
            current_balance = _get_points(BOT_MASTER)
        except Exception:
            current_balance = 0
        if english:
            lines = [
                f"🧪 Farm check — simulated crop {index + 1}/{len(crops)}",
                f"🌱 Crop: {crop}", f"⏱️ Growing time: {minutes} minutes",
                f"🎁 Expected harvest: +{_fmt_points(reward)} points",
                f"💳 Simulated balance after harvest: {_fmt_points(current_balance + reward)}",
                "⚠️ Preview only: no crop was planted and no points were changed.",
            ]
            lines.append(
                "✅ End of crop list; the next check starts again from the first crop."
                if index == len(crops) - 1 else "📩 Send farm check again for the next crop."
            )
        else:
            lines = [
                f"🧪 فحص زرع — محاكاة المحصول {index + 1}/{len(crops)}",
                f"🌱 المحصول: {crop}", f"⏱️ مدة الزراعة: {minutes} دقيقة",
                f"🎁 المكافأة المتوقعة: +{_fmt_points(reward)} نقطة",
                f"💳 الرصيد الافتراضي بعد الحصاد: {_fmt_points(current_balance + reward)}",
                "⚠️ معاينة فقط: لم تتم زراعة محصول ولم يتغير رصيدك.",
            ]
            lines.append(
                "✅ انتهت قائمة المحاصيل؛ يبدأ الفحص التالي من أول محصول."
                if index == len(crops) - 1 else "📩 أرسل «فحص زرع» للمحصول التالي."
            )
        self.send_private_text(BOT_MASTER, "\n".join(lines))
        return True

    def _route_room_game_input(self, room, username, text, username_aliases=()):
        """Route rank queries and active game commands."""
        if self._handle_user_room_rank_command(room, username, text):
            return True
        if self._handle_bot_room_role_command(room, username, text):
            return True
        candidates, seen = [], set()
        for candidate in (username, *(username_aliases or ())):
            candidate = str(candidate or "").strip().lstrip("@")
            key = _norm_user(candidate)
            if candidate and key and key not in seen:
                seen.add(key)
                candidates.append(candidate)
        for candidate in candidates:
            if self._handle_bomb_wire(room, candidate, text):
                return True
        if self._handle_auction_room_command(room, username, text):
            return True
        if self._route_active_board_game_join(room, username, text):
            return True
        return self._cricket.handle(room, username, text, is_private=False)

    def _auction_connected_rooms(self, fallback_room="", *, rooms=None):
        """Return live room connections only; never broadcast to saved rooms."""
        connected = list(getattr(self, "connected_rooms", set()) or ())
        if rooms is None:
            candidates = connected[:]
            if fallback_room:
                candidates.append(str(fallback_room).strip())
        else:
            requested = { _norm_room(name).casefold() for name in rooms if str(name or "").strip() }
            candidates = [name for name in connected if _norm_room(name).casefold() in requested]
            if fallback_room and _norm_room(fallback_room).casefold() in requested:
                candidates.append(str(fallback_room).strip())
        result, seen = [], set()
        for candidate in candidates:
            name = str(candidate or "").strip()
            key = _norm_room(name).casefold()
            if not name or not key or key in seen:
                continue
            seen.add(key)
            if _games_enabled_for_room(name):
                result.append(name)
        return result

    def _auction_participant_rooms(self, auction=None):
        if auction is None:
            auction_game = getattr(self, "_auction_game", None)
            auction = auction_game.active() if auction_game else None
        if not isinstance(auction, dict):
            return []
        fallback_room = str(auction.get("room") or "").strip()
        rooms = [str(player.get("room") or fallback_room).strip() for player in auction.get("participants", []) if isinstance(player, dict)]
        return self._auction_connected_rooms(rooms=rooms)

    @staticmethod
    def _auction_deadline_at(auction):
        try:
            started_at = float(auction.get("started_at") or 0)
            return float(auction.get("deadline_at") or (started_at + BIDDING_DURATION_SECONDS))
        except (AttributeError, TypeError, ValueError):
            return 0.0

    def _cancel_auction_timeout(self, auction_id=None):
        lock = getattr(self, "_auction_timeout_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._auction_timeout_lock = lock
        with lock:
            timer = getattr(self, "_auction_timer", None)
            timer_id = getattr(self, "_auction_timer_id", None)
            if timer is None or (auction_id is not None and str(timer_id or "") != str(auction_id)):
                return False
            self._auction_timer = None
            self._auction_timer_id = None
            try:
                timer.cancel()
            except Exception:
                pass
            return True

    def _schedule_auction_timeout(self, auction=None):
        game = getattr(self, "_auction_game", None)
        if auction is None and game is not None:
            auction = game.active()
        if not isinstance(auction, dict) or auction.get("phase") != "bidding":
            self._cancel_auction_timeout()
            return False
        auction_id = str(auction.get("id") or "")
        if not auction_id:
            return False
        deadline_at = self._auction_deadline_at(auction)
        try:
            turn_deadline_at = float(auction.get("turn_deadline_at") or (float(auction.get("started_at") or 0) + TURN_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            turn_deadline_at = deadline_at
        try:
            now = float(game.clock())
        except Exception:
            now = time.time()
        delay = max(0.0, min(deadline_at, turn_deadline_at) - now)
        lock = getattr(self, "_auction_timeout_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._auction_timeout_lock = lock
        timer = threading.Timer(delay, self._expire_auction_round, args=(auction_id,))
        timer.daemon = True
        with lock:
            previous = getattr(self, "_auction_timer", None)
            if previous is not None:
                try:
                    previous.cancel()
                except Exception:
                    pass
            self._auction_timer = timer
            self._auction_timer_id = auction_id
            timer.start()
        return True

    def _expire_auction_round(self, auction_id):
        game = getattr(self, "_auction_game", None)
        active = game.active() if game is not None else None
        if not isinstance(active, dict) or str(active.get("id") or "") != str(auction_id) or active.get("phase") != "bidding":
            self._cancel_auction_timeout(auction_id)
            return False
        deadline_at = self._auction_deadline_at(active)
        try:
            now = float(game.clock())
        except Exception:
            now = time.time()
        rooms = self._auction_participant_rooms(active)
        if now >= deadline_at:
            self._cancel_auction_timeout(auction_id)
            result = game.close(
                str(active.get("opener_id") or ""),
                str(active.get("opener_name") or ""),
                apply_delta=_apply_points_delta_once,
            )
            if not self._auction_broadcast_close_result(result, rooms=rooms, expired=True) and result.get("error"):
                self._auction_broadcast(
                    "⌛ انتهت مدة المزايدة، لكن تعذرت التسوية الآن؛ لم يُعلن فائز.",
                    "⌛ The 60-second bidding window ended, but settlement failed; no winner was announced.",
                    rooms=rooms,
                )
            return True

        try:
            turn_deadline_at = float(active.get("turn_deadline_at") or (float(active.get("started_at") or 0) + TURN_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            turn_deadline_at = deadline_at
        if now < turn_deadline_at:
            self._schedule_auction_timeout(active)
            return False

        timed_out_user_id = str(active.get("turn_user_id") or "")
        timed_out_username = str(active.get("turn_username") or "")
        result = game.pass_turn(
            timed_out_user_id,
            timed_out_username,
            apply_delta=_apply_points_delta_once,
        )
        if result.get("status") == "passed":
            next_turn = result.get("next_turn_username") or ""
            self._auction_broadcast(
                f"⏱️ انتهت مهلة 10 ثوانٍ دون مزايدة من @{timed_out_username}؛ تم تمرير دوره تلقائيًا. الدور التالي على @{next_turn}.",
                f"⏱️ @{timed_out_username} did not bid within 10 seconds; the turn was passed automatically. Next turn: @{next_turn}.",
                rooms=rooms,
            )
            self._schedule_auction_timeout(result.get("auction"))
            return True
        if result.get("auto_closed") or result.get("pending") or result.get("status") in {"sold", "no_bids", "no_sale"}:
            self._cancel_auction_timeout(auction_id)
            self._auction_broadcast(
                f"⏱️ انتهت مهلة @{timed_out_username} دون مزايدة؛ تم تمرير دوره تلقائيًا.",
                f"⏱️ @{timed_out_username} did not bid before the turn timer; the turn passed automatically.",
                rooms=rooms,
            )
            self._auction_broadcast_close_result(result, rooms=rooms)
            return True
        latest = game.active()
        if isinstance(latest, dict) and latest.get("phase") == "bidding":
            self._schedule_auction_timeout(latest)
        return False

    def _auction_broadcast(self, arabic_text, english_text, fallback_room="", *, rooms=None):
        target_rooms = self._auction_connected_rooms(fallback_room, rooms=rooms) if rooms is not None else self._auction_connected_rooms(fallback_room)
        for target_room in target_rooms:
            try:
                language = self._effective_room_language(target_room)
            except Exception:
                language = "ar"
            self.send_room_text(target_room, english_text if language == "en" else arabic_text)

    def _auction_room_reply(self, room, arabic_text, english_text):
        try:
            language = self._effective_room_language(room)
        except Exception:
            language = "ar"
        return self.send_room_text(room, english_text if language == "en" else arabic_text)

    def _auction_broadcast_close_result(self, result, fallback_room="", *, rooms=None, expired=False):
        prefix_ar = "⏰ انتهت مدة المزايدة (60 ثانية).\n" if expired else ""
        prefix_en = "⏰ The 60-second bidding window ended.\n" if expired else ""
        if result.get("status") == "no_bids":
            self._auction_broadcast(f"{prefix_ar}🔔 أُغلق المزاد دون عروض؛ لم تُخصم نقاط ولم تُمنح السلعة.", f"{prefix_en}🔔 The auction closed without bids; no points were charged and the item was not awarded.", fallback_room, rooms=rooms)
        elif result.get("status") == "no_sale":
            self._auction_broadcast(f"{prefix_ar}⚠️ لم يكتمل البيع لأن رصيد أعلى مزايد لم يعد كافياً؛ لم تُخصم نقاط ولم تُمنح السلعة.", f"{prefix_en}⚠️ The sale could not complete because the highest bidder no longer had enough points; no points were charged and the item was not awarded.", fallback_room, rooms=rooms)
        elif result.get("status") == "sold":
            sale = result["result"]
            item = sale["item"]
            short_id = str(sale["auction_id"])[:8]
            self._auction_broadcast(
                f"{prefix_ar}🏆 انتهى المزاد! فاز @{sale['winner']} بـ{item.get('icon', '💎')} {item.get('name_ar', 'الجائزة')} بسعر {_fmt_points(sale['bid'])} نقطة.\n✅ أضيفت إلى المخزون؛ الاسترداد بالحظ: 1% ربح، 1% خسارة، و98% دون تغيير.\nللمخزون: sv — للاسترداد: red auction {short_id}",
                f"{prefix_en}🏆 Auction complete! @{sale['winner']} won {item.get('icon', '💎')} {item.get('name_en', 'the prize')} for {_fmt_points(sale['bid'])} points.\n✅ Added to inventory; redemption odds: 1% profit, 1% loss, and 98% no change.\nInventory: sv — redeem: red auction {short_id}",
                fallback_room,
                rooms=rooms,
            )
        elif result.get("pending"):
            self._auction_broadcast(f"{prefix_ar}⌛ التسوية معلقة مؤقتاً؛ لم يُعلن الفائز بعد. أعد «اقفل المزاد» لاحقاً أو أعد تشغيل البوت للمحاولة بأمان دون خصم مكرر.", f"{prefix_en}⌛ Settlement is pending; no winner is announced yet. Retry “close auction” later or restart the bot; the transaction ID prevents a duplicate charge.", fallback_room, rooms=rooms)
        else:
            return False
        return True

    def _auction_send_item_image(self, item, fallback_room=""):
        filename = str((item or {}).get("image") or "").strip()
        image_path = ASSETS_DIR / "treasures" / filename
        if not filename or not image_path.is_file():
            self.log("[AUCTION] collectible image is missing:", filename)
            return False
        try:
            if image_path.stat().st_size > MAX_IMAGE_BYTES:
                self.log("[AUCTION] collectible image exceeds 50 KB:", filename, image_path.stat().st_size)
                return False
        except OSError as exc:
            self.log("[AUCTION] cannot inspect collectible image:", filename, repr(exc))
            return False
        image_url = self._game_public_image(image_path, route="assets/treasures")
        if not image_url:
            return False
        item_name_ar = str((item or {}).get("name_ar") or (item or {}).get("name_en") or "قطعة المزاد")
        item_name_en = str((item or {}).get("name_en") or (item or {}).get("name_ar") or "Auction item")
        sent = False
        for target_room in self._auction_connected_rooms(fallback_room):
            try:
                language_en = self._effective_room_language(target_room) == "en"
                if language_en:
                    warning = (
                        f"🔨 Auction item: {item_name_en}\n"
                        "⚠️ Not every auction item may be authentic. Be careful not to risk your entire fortune."
                    )
                else:
                    warning = (
                        f"🔨 قطعة المزاد: {item_name_ar}\n"
                        "⚠️ قد لا تكون كل قطع المزاد أصلية؛ فاحذر أن تغامر بكل ثروتك."
                    )
                self.send_room_text(target_room, warning)
            except Exception as exc:
                self.log("[AUCTION] item warning delivery failed:", target_room, repr(exc))
            try:
                if self.send_room_media(target_room, image_url, "image") is not False:
                    sent = True
            except Exception as exc:
                self.log("[AUCTION] image delivery failed:", target_room, repr(exc))
        return sent

    def _handle_auction_toggle(self, sender, text, *, is_private=False, room=""):
        enabled = _parse_auction_toggle_command(text)
        if enabled is None:
            return False
        if not _is_primary_master(sender):
            notice = "🔒 تشغيل وإيقاف المزاد العالمي مخصص للماستر الأساسي فقط."
            if is_private:
                self.send_private_text(sender, notice)
            elif room:
                self.send_room_text(room, notice)
            return True
        participant_rooms = self._auction_participant_rooms()
        result = self._auction_game.set_enabled(enabled)
        if enabled:
            self.send_private_text(sender, "✅ تم تشغيل لعبة المزاد. اكتب «افتح مزاد» في غرفة متصلة لبدء جولة عالمية.")
        else:
            self._cancel_auction_timeout()
            self._auction_broadcast("⛔ أوقف الماستر لعبة المزاد؛ أُلغيت الجولة المفتوحة دون خصم نقاط.", "⛔ The master disabled the auction game; the open round was cancelled without charging points.", rooms=participant_rooms)
            extra = " أُلغيت الجولة المفتوحة دون خصم." if result.get("cancelled_active") else ""
            self.send_private_text(sender, f"⛔ تم إيقاف لعبة المزاد.{extra}")
        return True

    def _handle_auction_private_command(self, sender, text):
        return self._handle_auction_toggle(sender, text, is_private=True)

    def _handle_auction_room_command(self, room, username, text):
        if self._handle_auction_toggle(username, text, is_private=False, room=room):
            return True
        auction_game = getattr(self, "_auction_game", None)
        active_auction = auction_game.active() if auction_game else None
        # A bare number is an auction bid only while an actual bidding round
        # is live. Outside that phase it must fall through to normal room
        # handling; otherwise every ordinary message such as 12345 receives
        # the misleading "auction closed" reply.
        normalized_number = str(text or "").strip().translate(
            str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
        )
        bare_number = bool(re.fullmatch(r"[0-9]+(?:[mMbB]|[مب])?", normalized_number))
        if bare_number and not (
            isinstance(active_auction, dict)
            and active_auction.get("phase") == "bidding"
        ):
            return False
        action = _parse_auction_room_command(text)
        if not action:
            return False
        if not active_auction and action in {"join", "bid", "pass"}:
            self._auction_room_reply(room, "📭 المزاد مغلق؛ انتظر بدء جولة أخرى.", "📭 The auction is closed; wait for another round to start.")
            return True
        if isinstance(active_auction, dict) and active_auction.get("phase") == "bidding":
            game_clock = getattr(auction_game, "clock", time.time)
            if float(game_clock()) >= self._auction_deadline_at(active_auction):
                participant_rooms = self._auction_participant_rooms(active_auction)
                source_is_participant = _norm_room(room).casefold() in {_norm_room(name).casefold() for name in participant_rooms}
                self._expire_auction_round(active_auction.get("id"))
                if action in {"join", "bid", "pass"} and not source_is_participant:
                    self._auction_room_reply(room, "📭 المزاد مغلق؛ انتظر بدء جولة أخرى.", "📭 The auction is closed; wait for another round to start.")
                return True
            current_user_id = "name:" + _norm_user(username)
            participants = active_auction.get("participants") or []
            registered = any(str(player.get("user_id") or "") == current_user_id for player in participants if isinstance(player, dict))
            participant_rooms = self._auction_participant_rooms(active_auction)
            if action in {"join", "bid", "pass"} and _norm_room(room).casefold() not in {_norm_room(name).casefold() for name in participant_rooms}:
                self._auction_room_reply(room, "📭 المزاد مغلق في هذه الغرفة؛ انتظر انتهاء الجولة وبدء جولة أخرى.", "📭 The auction is closed to this room; wait for the current round to end and another round to open.")
                return True
            if action == "join":
                self._auction_room_reply(room, "⌛ يوجد مزاد شغال الآن؛ التسجيل مغلق. انتظر انتهاء الجولة لفتح التسجيل مجددًا.", "⌛ An auction is already running; registration is closed. Wait until the round ends for the next one.")
                return True
            if action in {"bid", "pass"} and (not registered or current_user_id != str(active_auction.get("turn_user_id") or "")):
                return True
        if not _is_verified_user(username):
            self.send_room_text(room, f"🔒 @{username} حسابك غير موثق للمشاركة في الألعاب.\n{_verification_notice()}")
            return True
        if not _games_enabled_for_room(room):
            self.send_room_text(room, "🛑 الألعاب متوقفة في هذه الغرفة حالياً.")
            return True
        user_id = "name:" + _norm_user(username)
        is_master = _is_primary_master(username)
        if action == "open":
            result = self._auction_game.open_auction(user_id, username, room)
            if not result.get("ok"):
                errors = {
                    "disabled": ("⛔ لعبة المزاد متوقفة؛ اطلب من الماستر تشغيلها.", "⛔ The auction game is disabled; ask the master to enable it."),
                    "already_open": ("⌛ توجد جولة مزاد عالمية مفتوحة بالفعل.", "⌛ A world auction is already open."),
                }
                ar, en = errors.get(result.get("error"), ("❌ تعذر فتح المزاد.", "❌ The auction could not be opened."))
                self._auction_room_reply(room, ar, en)
                return True
            auction = result["auction"]
            item = auction["item"]
            item_ar = f"{item['icon']} {item['name_ar']}"
            item_en = f"{item['icon']} {item['name_en']}"
            starting_price = _fmt_points(item.get("starting_price", 100_000_000))
            self._auction_broadcast(
                f"🔨 بدأ التسجيل في المزاد العالمي!\n🎁 السلعة المختارة: {item_ar}\n💰 السعر الابتدائي: {starting_price} نقطة.\n👥 الحد الأقصى 5 مشاركين؛ اكتب «مزايده» للتسجيل. عند اكتمال 3 يستطيع فاتح الجولة كتابة «ابدا المزاد»، أو يبدأ تلقائياً عند 5. بعد البدء تكون المزايدة بالدور لمدة 60 ثانية؛ لكل لاعب 10 ثوانٍ لإرسال المبلغ قبل انتقال الدور تلقائيًا. اكتب الرقم فقط مثل 110m أو 10b أو 120000000؛ أو «تمرير».",
                f"🔨 Registration is open for the world auction!\n🎁 Selected item: {item_en}\n💰 Starting price: {starting_price} points.\n👥 Up to 5 players; type “join auction” to register. The host may start with 3, or it starts automatically at 5. After registration, bidding runs in turns for 60 seconds, with 10 seconds per turn before it advances automatically. Send only the number, such as 110m or 10b, or type “pass”.",
                room,
            )
            if not self._auction_send_item_image(item, room):
                self._auction_broadcast("⚠️ تعذر إرسال صورة السلعة؛ لم يتجاوز ملفها حد 50 كيلوبايت.", "⚠️ The collectible image could not be sent; the asset must be no larger than 50 KB.", room)
            return True

        if action == "join":
            result = self._auction_game.join(user_id, username, room=room)
            if not result.get("ok"):
                errors = {
                    "no_auction": ("📭 لا توجد جولة مزاد مفتوحة للتسجيل.", "📭 No auction is open for registration."),
                    "registration_closed": ("⌛ يوجد مزاد شغال الآن؛ انتظر انتهاء الجولة لفتح التسجيل مجددًا.", "⌛ An auction is running; wait until the round ends for registration to reopen."),
                    "full": ("👥 اكتمل العدد الأقصى للمشاركين.", "👥 The auction has reached its player limit."),
                }
                ar, en = errors.get(result.get("error"), ("❌ تعذر تسجيلك في المزاد.", "❌ Could not register you for the auction."))
                self._auction_room_reply(room, ar, en)
                return True
            participant_rooms = self._auction_participant_rooms(result.get("auction"))
            if result.get("already_joined"):
                self._auction_broadcast(f"✅ @{username} مسجل بالفعل في المزاد.", f"✅ @{username} is already registered for the auction.", rooms=participant_rooms)
            else:
                count = result.get("participant_count", 0)
                self._auction_broadcast(f"✅ سُجل @{username} في المزاد ({count}/5 مشاركين).", f"✅ @{username} joined the auction ({count}/5 players).", rooms=participant_rooms)
            if result.get("auto_started"):
                self._schedule_auction_timeout(result.get("auction"))
                item = result["auction"].get("item") or {}
                starting_price = _fmt_points(item.get("starting_price", 100_000_000))
                first_turn = result["auction"].get("turn_username") or ""
                self._auction_broadcast(f"🔔 اكتمل العدد وبدأت المزايدة لمدة 60 ثانية. السعر الابتدائي {starting_price} نقطة؛ أول دور على @{first_turn}. لكل لاعب 10 ثوانٍ للمزايدة أو التمرير، ثم ينتقل الدور تلقائيًا.", f"🔔 Player limit reached; 60-second bidding has started. Starting price: {starting_price} points. First turn: @{first_turn}. Each player has 10 seconds to bid or pass before the turn advances automatically.", rooms=self._auction_participant_rooms(result.get("auction")))
            return True

        if action == "start":
            result = self._auction_game.start(user_id, username, is_master=is_master)
            if not result.get("ok"):
                errors = {
                    "no_auction": ("📭 لا يوجد مزاد مفتوح.", "📭 No auction is open."),
                    "not_opener": ("🔒 بدء المزايدة المبكر متاح لفاتح الجولة أو الماستر فقط.", "🔒 Only the host or master can start bidding early."),
                    "not_enough_players": (f"👥 يلزم تسجيل {result.get('participant_count', 0)}/3 مشاركين على الأقل.", f"👥 At least 3 players must register ({result.get('participant_count', 0)}/3)."),
                    "start_cooldown": (f"⏳ لا يمكنك تشغيل المزاد مرة أخرى الآن؛ تبقى {result.get('remaining_seconds', 0)} ثانية (ساعة بين تشغيلتين).", f"⏳ You must wait {result.get('remaining_seconds', 0)} more seconds before starting another auction (one hour between starts)."),
                }
                ar, en = errors.get(result.get("error"), ("🔒 التسجيل مغلق أو بدأت المزايدة بالفعل.", "🔒 Registration is closed or bidding has already started."))
                self._auction_room_reply(room, ar, en)
                return True
            self._schedule_auction_timeout(result.get("auction"))
            item = result["auction"].get("item") or {}
            starting_price = _fmt_points(item.get("starting_price", 100_000_000))
            first_turn = result["auction"].get("turn_username") or ""
            self._auction_broadcast(f"🔔 أغلق فاتح الجولة التسجيل؛ بدأت المزايدة لمدة 60 ثانية. السعر الابتدائي {starting_price} نقطة؛ أول دور على @{first_turn}. لكل لاعب 10 ثوانٍ للمزايدة أو التمرير، ثم ينتقل الدور تلقائيًا.", f"🔔 The host closed registration; 60-second bidding starts now. Starting price: {starting_price} points. First turn: @{first_turn}. Each player has 10 seconds to bid or pass before the turn advances automatically.", rooms=self._auction_participant_rooms(result.get("auction")))
            return True

        if action == "bid":
            bid_amount = _parse_auction_bid_amount(text)
            if bid_amount is None:
                self._auction_room_reply(room, "أرسل رقم المزايدة فقط، مثل: 120m أو 10b أو 120000000.", "Send only the bid number, for example: 120m, 10b, or 120000000.")
                return True
            result = self._auction_game.bid(user_id, username, bid_amount, _get_points(username))
            if not result.get("ok"):
                if result.get("error") in {"expired", "turn_expired"}:
                    self._expire_auction_round(active_auction.get("id"))
                    return True
                if result.get("error") in {"not_your_turn", "not_participant"}:
                    return True
                errors = {
                    "no_auction": ("📭 لا يوجد مزاد مفتوح.", "📭 No auction is open."),
                    "not_bidding": ("⌛ المزايدة لم تبدأ أو انتهت.", "⌛ Bidding has not started or has ended."),
                    "not_participant": ("🔒 سجّل أولاً أثناء التسجيل بكتابة «مزايده»؛ المشاركة بالمزايدة للمسجلين فقط.", "🔒 Register first by typing “join auction”; only registered players may bid."),
                    "invalid_amount": ("❌ مبلغ المزايدة غير صحيح. استخدم رقمًا مثل 120m أو 10b أو 120000000.", "❌ Invalid bid amount. Use a number such as 110m or 10b."),
                    "bid_too_low": (f"❌ أقل مزايدة مقبولة الآن هي {_fmt_points(result.get('required', 0))} نقطة.", f"❌ The minimum accepted bid right now is {_fmt_points(result.get('required', 0))} points."),
                    "insufficient": (f"❌ تحتاج {_fmt_points(result.get('required', 0))} نقطة للمزايدة؛ رصيدك {_fmt_points(result.get('balance', 0))}.", f"❌ You need {_fmt_points(result.get('required', 0))} points to bid; your balance is {_fmt_points(result.get('balance', 0))}."),
                }
                ar, en = errors.get(result.get("error"), ("❌ تعذرت المزايدة.", "❌ The bid could not be placed."))
                self._auction_room_reply(room, ar, en)
                return True
            amount = _fmt_points(result["amount"])
            next_turn = result.get("next_turn_username") or ""
            self._schedule_auction_timeout(result.get("auction"))
            self._auction_broadcast(f"🔨 زايد @{username} بمبلغ {amount} نقطة؛ هذا أعلى سعر حالي. الدور التالي على @{next_turn}.", f"🔨 @{username} bid {amount} points; this is the current high bid. Next turn: @{next_turn}.", rooms=self._auction_participant_rooms(result.get("auction")))
            return True

        if action == "pass":
            participant_rooms = self._auction_participant_rooms(active_auction)
            result = self._auction_game.pass_turn(user_id, username, apply_delta=_apply_points_delta_once)
            if result.get("error") == "expired":
                self._expire_auction_round(active_auction.get("id"))
                return True
            if result.get("status") == "passed":
                next_turn = result.get("next_turn_username") or ""
                self._schedule_auction_timeout(result.get("auction"))
                self._auction_broadcast(f"⏭️ مرّر @{username} دوره؛ الدور التالي على @{next_turn}.", f"⏭️ @{username} passed; next turn: @{next_turn}.", rooms=participant_rooms)
                return True
            if result.get("auto_closed") or result.get("pending") or result.get("status") in {"sold", "no_bids", "no_sale"}:
                self._cancel_auction_timeout(active_auction.get("id"))
            if not self._auction_broadcast_close_result(result, room, rooms=participant_rooms):
                if result.get("error") in {"not_your_turn", "not_participant"}:
                    return True
                errors = {
                    "no_auction": ("📭 لا يوجد مزاد مفتوح.", "📭 No auction is open."),
                    "not_bidding": ("⌛ لم تبدأ المزايدة بعد.", "⌛ Bidding has not started."),
                }
                ar, en = errors.get(result.get("error"), ("❌ تعذر تمرير الدور.", "❌ Could not pass the turn."))
                self._auction_room_reply(room, ar, en)
            return True

        if action == "close":
            participant_rooms = self._auction_participant_rooms()
            result = self._auction_game.close(user_id, username, is_master=is_master, apply_delta=_apply_points_delta_once)
            if result.get("ok") or result.get("pending") or result.get("status"):
                self._cancel_auction_timeout(active_auction.get("id") if active_auction else None)
            if not self._auction_broadcast_close_result(result, room, rooms=participant_rooms) and not result.get("ok"):
                errors = {
                    "no_auction": ("📭 لا يوجد مزاد مفتوح.", "📭 No auction is open."),
                    "not_opener": ("🔒 إغلاق المزاد متاح لفاتح الجولة أو الماستر فقط.", "🔒 Only the host or master may close the auction."),
                    "not_bidding": ("⌛ المزايدة لم تبدأ بعد.", "⌛ Bidding has not started yet."),
                }
                ar, en = errors.get(result.get("error"), ("❌ تعذر إغلاق المزاد.", "❌ The auction could not be closed."))
                self._auction_room_reply(room, ar, en)
            return True

        if action == "cancel":
            participant_rooms = self._auction_participant_rooms()
            result = self._auction_game.cancel(user_id, username, is_master=is_master)
            if result.get("ok"):
                self._cancel_auction_timeout(active_auction.get("id") if active_auction else None)
                self._auction_broadcast("⛔ أُلغيت جولة المزاد دون خصم نقاط.", "⛔ The auction round was cancelled without charging points.", rooms=participant_rooms)
            else:
                errors = {"no_auction": ("📭 لا يوجد مزاد مفتوح.", "📭 No auction is open."), "not_opener": ("🔒 إلغاء المزاد متاح لفاتح الجولة أو الماستر فقط.", "🔒 Only the host or master may cancel the auction."), "settlement_in_progress": ("⌛ تجري تسوية المزاد ولا يمكن إلغاؤه الآن.", "⌛ Auction settlement is in progress and cannot be cancelled.")}
                ar, en = errors.get(result.get("error"), ("❌ تعذر إلغاء المزاد.", "❌ The auction could not be cancelled."))
                self._auction_room_reply(room, ar, en)
            return True
        return False

    def _fishing_event_worker(self):
        interval = 600.0
        while not self.stop_event.wait(interval):
            try:
                self._start_fishing_event()
            except Exception as exc:
                self.log("[FISHING] event start failed; scheduler will continue:", repr(exc))

    def _is_dragon_hunter(self, username):
        key = _norm_user(str(username or "").strip().lstrip("@"))
        if not key:
            return False
        lock = getattr(self, "_dragon_hunters_lock", None)
        if lock is None:
            self._dragon_hunters_lock = lock = threading.RLock()
        with lock:
            hunters = getattr(self, "dragon_hunters", {})
            entry = hunters.get(key) if isinstance(hunters, dict) else None
            if not isinstance(entry, dict):
                return False
            expiry = float(entry.get("expires_at") or (float(entry.get("awarded_at") or 0) + 600.0))
            return time.time() < expiry

    def _record_dragon_hunter(self, username, room):
        username = str(username or "").strip().lstrip("@")
        key = _norm_user(username)
        if not key:
            return False
        lock = getattr(self, "_dragon_hunters_lock", None)
        if lock is None:
            self._dragon_hunters_lock = lock = threading.RLock()
        with lock:
            hunters = getattr(self, "dragon_hunters", {})
            hunters = dict(hunters) if isinstance(hunters, dict) else {}
            hunters[key] = {
                "username": username,
                "room": str(room or ""),
                "awarded_at": int(time.time()),
                "expires_at": int(time.time()) + 600,
            }
            self.dragon_hunters = hunters
            try:
                _save_local_json(DRAGON_HUNTERS_FILE, {"version": 1, "hunters": hunters})
            except Exception as exc:
                self.log("[FISHING] dragon hunter badge save failed:", username, repr(exc))
                return False
        return True

    def _broadcast_dragon_hunter_result(self, username, room, reward, event_id=None):
        # Do not let a fast winner overtake a slow room's opening notice.
        # _start_fishing_event records this only after every opening send has
        # returned from the socket.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with getattr(self, "_fishing_lock", threading.RLock()):
                current = getattr(self, "_fishing_event", None)
                if not event_id or not isinstance(current, dict) or current.get("id") != event_id:
                    break
                completed_at = float(current.get("opening_complete_at", 0) or 0)
                if completed_at:
                    # Small network/socket settling gap prevents a winner
                    # frame from being queued beside the final opening frame.
                    remaining = 0.35 - (time.monotonic() - completed_at)
                    if remaining > 0:
                        time.sleep(min(remaining, 0.05))
                        continue
                    break
            time.sleep(0.02)
        try:
            rooms = set(self._active_rooms())
        except Exception:
            rooms = {str(item).strip() for item in getattr(self, "connected_rooms", set()) if str(item).strip()}
        rooms.add(str(room or "").strip())
        target_rooms = sorted(
            {item for item in rooms if item and not _game_results_disabled_for_room(item)},
            key=str.casefold,
        )

        def announce(target_room):
            room_language = self._configured_room_language(target_room)
            if room_language == "en":
                result_text = (
                    f"🐉🏆 @{username} caught the dragon!\n"
                    f"🏠 Hunting room: {room}\n💰 Reward: +{_fmt_points(reward)} points"
                )
            else:
                result_text = (
                    f"🐉🏆 @{username} اصطاد التنين!\n"
                    f"🏠 غرفة الصيد: {room}\n💰 الجائزة: +{_fmt_points(reward)} نقطة"
                )
            try:
                self.send_room_text(target_room, result_text)
            except Exception as exc:
                self.log("[FISHING] dragon announcement failed:", target_room, repr(exc))

        # Deliver to all active rooms concurrently. send_query's socket send
        # lock still serializes individual frames safely, but no room waits on
        # a previous room's network/encoding work.
        if target_rooms:
            with ThreadPoolExecutor(max_workers=min(8, len(target_rooms)), thread_name_prefix="dragon-result") as pool:
                list(pool.map(announce, target_rooms))

    def _queue_dragon_hunter_broadcast(self, username, room, reward, event_id=None):
        worker = threading.Thread(
            target=self._broadcast_dragon_hunter_result,
            args=(username, room, reward, event_id),
            daemon=True,
            name="dragon-hunter-announcement",
        )
        worker.start()

    def _start_fishing_event(self, now=None):
        now = time.time() if now is None else float(now)
        lock = getattr(self, "_fishing_lock", None)
        if lock is None:
            self._fishing_lock = lock = threading.RLock()
        with lock:
            self._fishing_event_seq = int(getattr(self, "_fishing_event_seq", 0)) + 1
            event = {"id": self._fishing_event_seq, "started_at": now, "ends_at": now + 600.0, "announced": False}
            self._fishing_event = event
            self._fishing_shots = set()
            self._fishing_pending_shots = set()
            self._fishing_dragon_pending = ""
            self._fishing_dragon_caught = False
        try:
            rooms = self._active_rooms()
        except Exception:
            rooms = sorted({str(room).strip() for room in getattr(self, "connected_rooms", set()) if str(room).strip()}, key=str.casefold)
        rooms = [target_room for target_room in rooms if not _game_results_disabled_for_room(target_room)]
        opening_barrier = threading.Barrier(len(rooms)) if rooms else None
        def announce_open(target_room):
            if self._configured_room_language(target_room) == "en":
                announcement = (
                    "🎯 Ordinary Shot round is open!\n"
                    "🐉 Dragon spotted — send Shot to catch it for 1,000,000 points."
                )
            else:
                announcement = (
                    "🎯 جولة الشوت العادي بدأت!\n"
                    "🐉 ظهر التنين — أرسل «شوت» فقط لصيده والفوز بمليون نقطة."
                )
            try:
                if opening_barrier is not None:
                    opening_barrier.wait(timeout=10.0)
                with lock:
                    current = getattr(self, "_fishing_event", None)
                    if (
                        not isinstance(current, dict)
                        or current.get("id") != event.get("id")
                        or current.get("winner")
                        or current.get("opening_cancelled")
                    ):
                        return
                self._send_game_open_announcement(target_room, announcement)
            except Exception as exc:
                self.log("[FISHING] dragon event announcement failed:", target_room, repr(exc))

        if rooms:
            with ThreadPoolExecutor(max_workers=max(1, len(rooms)), thread_name_prefix="dragon-open") as pool:
                list(pool.map(announce_open, rooms))
        with lock:
            current = getattr(self, "_fishing_event", None)
            if isinstance(current, dict) and current.get("id") == event.get("id"):
                current["announced"] = True
                current["opening_complete_at"] = time.monotonic()
        self.log("[FISHING] round announcement barrier released", event.get("id"), "rooms=", len(rooms))
        self.log("[FISHING] event opened", self._fishing_event_seq, "rooms=", len(rooms))
        return event

    def _fishing_shoot(self, room, sender):
        username = str(sender or "").strip().lstrip("@")
        user_key = _norm_user(username)
        now = time.time()
        lock = getattr(self, "_fishing_lock", None)
        if lock is None:
            self._fishing_lock = lock = threading.RLock()
        previous_cooldown = None
        previous_cooldown_present = False
        cooldown_remaining = 0.0
        with lock:
            event = getattr(self, "_fishing_event", None)
            if isinstance(event, dict) and not event.get("announced", True) and now < float(event.get("ends_at", 0)):
                self.send_room_text(room, "⏳ يجري نشر إعلان جولة الشوت في جميع الغرف؛ أعد المحاولة بعد لحظة.")
                return True
            if not isinstance(event, dict) or now < float(event.get("started_at", 0)) or now >= float(event.get("ends_at", 0)):
                event_id = None
            else:
                event_id = int(event.get("id", 0))
            shot_key = (event_id, user_key)
            pending = getattr(self, "_fishing_pending_shots", set())
            cooldowns = getattr(self, "_fishing_cooldowns", {})
            if not isinstance(cooldowns, dict):
                cooldowns = {}
            last_shot = float(cooldowns.get(user_key, 0) or 0)
            if shot_key in pending:
                busy = True
                dragon = False
            elif (not _is_env_master_for_game_cooldown(username)) and FISHING_SHOT_COOLDOWN_SECONDS > 0 and last_shot and now - last_shot < FISHING_SHOT_COOLDOWN_SECONDS:
                busy = False
                cooldown_remaining = FISHING_SHOT_COOLDOWN_SECONDS - (now - last_shot)
                dragon = False
            else:
                busy = False
                pending.add(shot_key)
                self._fishing_pending_shots = pending
                previous_cooldown_present = user_key in cooldowns
                previous_cooldown = cooldowns.get(user_key)
                cooldowns[user_key] = now
                if len(cooldowns) > 2048:
                    cooldowns = {key: stamp for key, stamp in cooldowns.items() if now - float(stamp or 0) < 3600}
                    self._fishing_cooldowns = cooldowns
                else:
                    self._fishing_cooldowns = cooldowns
                dragon_pending = str(getattr(self, "_fishing_dragon_pending", "") or "")
                dragon = event_id is not None and not bool(getattr(self, "_fishing_dragon_caught", False)) and not dragon_pending
                if dragon:
                    self._fishing_dragon_pending = user_key

        language_getter = getattr(self, "_configured_room_language", None) or self._effective_room_language
        language = language_getter(room)
        if busy:
            text = "⏳ جاري احتساب محاولتك، أعد الإرسال بعد لحظة." if language != "en" else "⏳ Your shot is processing; retry in a moment."
            self.send_room_text(room, text)
            return True
        if cooldown_remaining > 0:
            seconds = max(1, int(cooldown_remaining + 0.999))
            text = (
                f"⏳ فاصل الصيد دقيقتان؛ انتظر {seconds} ثانية قبل المحاولة التالية."
                if language != "en" else
                f"⏳ There is a 2-minute cooldown between shots; try again in {seconds} seconds."
            )
            self.send_room_text(room, text)
            return True

        animals = (
            ("🐇", "أرنب بري", "Wild rabbit", 5_000),
            ("🦊", "ثعلب", "Fox", 5_000),
            ("🦌", "غزال", "Deer", 5_000),
            ("🐗", "خنزير بري", "Wild boar", 5_000),
            ("🐻", "دب", "Bear", 5_000),
        )
        emoji, animal_ar, animal_en, reward = (
            ("🐉", "التنين النادر", "Rare dragon", 1_000_000)
            if dragon else secrets.choice(animals)
        )
        try:
            balance = self._game_award(username, reward)
        except Exception as exc:
            self.log("[FISHING] reward persistence failed:", username, repr(exc))
            with lock:
                self._fishing_pending_shots.discard(shot_key)
                cooldowns = getattr(self, "_fishing_cooldowns", {})
                if isinstance(cooldowns, dict) and cooldowns.get(user_key) == now:
                    if previous_cooldown_present:
                        cooldowns[user_key] = previous_cooldown
                    else:
                        cooldowns.pop(user_key, None)
                if dragon and getattr(self, "_fishing_dragon_pending", "") == user_key:
                    self._fishing_dragon_pending = ""
            text = "❌ تعذر حفظ المكافأة الآن؛ لم تُحتسب المحاولة، أعد شوت بعد قليل." if language == "ar" else "❌ The reward could not be saved; your shot was not counted. Please retry shortly."
            self.send_room_text(room, text)
            return True

        with lock:
            self._fishing_pending_shots.discard(shot_key)
            self._fishing_shots = getattr(self, "_fishing_shots", set())
            if event_id is not None:
                self._fishing_shots.add(shot_key)
            current_event = getattr(self, "_fishing_event", None)
            if dragon and isinstance(current_event, dict) and current_event.get("id") == event_id:
                self._fishing_dragon_pending = ""
                self._fishing_dragon_caught = True
        try:
            _record_game(username, "fishing_hunt", reward, 0)
        except Exception as exc:
            self.log("[FISHING] game statistics save failed:", repr(exc))

        if dragon:
            try:
                self._record_dragon_hunter(username, room)
            except Exception as exc:
                self.log("[FISHING] dragon hunter badge registration failed:", repr(exc))
            self._queue_dragon_hunter_broadcast(username, room, reward, event_id)
            threading.Thread(
                target=self._send_game_winner_card,
                args=("hunt_dragon", username, [room]),
                kwargs={"respect_room_filter": False},
                daemon=True,
                name="shot-winner-card",
            ).start()
            return True

        if language == "en":
            text = (
                f"🏹 Hunting result for @{username}\n🐾 Animal: {emoji} {animal_en}\n"
                f"💰 Reward: +{_fmt_points(reward)} points\n💳 Balance: {_fmt_points(balance)}"
            )
        else:
            text = (
                f"🏹 نتيجة رحلة الصيد لـ @{username}\n🐾 الحيوان: {emoji} {animal_ar}\n"
                f"💰 الجائزة: +{_fmt_points(reward)} نقطة\n💳 الرصيد: {_fmt_points(balance)}"
            )
        self.send_room_text(room, text)
        return True

    def _lottery_game(self, room, sender, amount=0):
        amount = int(amount or 0)
        if amount < 0:
            self.send_room_text(room, "❌ المبلغ غير صحيح.")
            return True
        if 0 < amount < WAGER_MIN_AMOUNT:
            message = (
                "❌ الحد الأدنى لرهان الحظ المدفوع هو 100k نقطة. اكتب حظ 100k أو أكثر."
                if self._effective_room_language(room) != "en"
                else "❌ The minimum paid-luck wager is 100k points. Enter luck 100k or more."
            )
            self.send_room_text(room, message)
            return True
        if not self._game_cooldown_notice(room, sender, WAGER_GAME_COOLDOWN_SECONDS, "حظ"):
            return True
        # جوائز الحظ تقع دائماً بين 5,000 و100,000 نقطة. في اللعب المدفوع
        # لا تُصرف الجائزة إلا عند الفوز، ويبقى مبلغ الرهان مستقلاً عن الجائزة.
        with self.game_lock:
            if amount:
                balance_before = _get_points(sender)
                if balance_before < amount:
                    self.send_room_text(room, f"❌ رصيدك غير كافٍ. رصيدك: {_fmt_points(balance_before)}")
                    return True
                # The stake is charged exactly once before the random draw.
                _add_points(sender, -amount)
            # Cryptographically strong random draw; the amount is never used as
            # the random seed and cannot force a matching payout.
            roll = secrets.randbelow(1000) + 1
            # الحظ المدفوع: البوت يدفع 10,000 فقط عند الفوز،
            # ولا يضرب مبلغ اللاعب في مضاعف.
            if amount:
                reward = secrets.randbelow(95_001) + 5_000 if roll <= 500 else 0
            else:
                reward = secrets.randbelow(95_001) + 5_000
            if reward:
                balance = self._game_award(sender, reward)
                result = f"🎉 ربحت: +{_fmt_points(reward)} نقطة\n💰 مبلغ الفوز: +{_fmt_points(reward)} نقطة"
            else:
                balance = _get_points(sender)
                result = "🍀 هذه الجولة لم تكن رابحة.\n💰 مبلغ الفوز: 0 نقطة"
            delta = reward - amount if amount else reward
            _record_game(sender, "luck_free" if not amount else "luck", delta, amount)
        text = _reply_template(
            "luck_result", DEFAULT_REPLY_MESSAGES["luck_result"],
            username=sender, result=result, amount=_fmt_points(amount),
            delta=("+" if delta >= 0 else "") + _fmt_points(delta),
            balance=_fmt_points(balance)
        )
        self.send_room_text(room, text)
        if reward > 0:
            self._send_game_winner_card("luck", sender, [room], respect_room_filter=False)
        return True

    def _stock_exchange_game(self, room, sender, choice=None):
        """Open the current investment market or collect an asset choice."""
        key = (_norm_room(room), _norm_user(sender))
        now = time.time()
        if choice is None:
            already_open = self._stock_market_is_open(now)
            event = self.stock_market_round if already_open else None
            if not already_open:
                event = self._start_stock_market_round(now, first_room=room)
            self.stock_pending[key] = {
                "kind": "market_choice", "room": room, "user": sender,
                "created": now, "event_id": event["id"],
            }
            self.send_room_text(room, self._stock_market_message(self._configured_room_language(room)))
            return True
        try:
            choice = int(choice)
        except (TypeError, ValueError):
            choice = 0
        if choice not in STOCK_MARKET_ASSETS:
            self.send_room_text(room, "❌ اختر 1 أو 2 أو 3 أو 4." if self._configured_room_language(room) != "en" else "❌ Choose 1, 2, 3, or 4.")
            return True
        event = self.stock_market_round
        if not event or not self._stock_market_is_open(now):
            self.stock_pending.pop(key, None)
            self.send_room_text(room, "⌛ انتهت جولة البورصة؛ اكتب بورصة لبدء جولة جديدة." if self._configured_room_language(room) != "en" else "⌛ The market round has closed; type stock to start another round.")
            return True
        self.stock_pending[key] = {
            "kind": "market_amount", "room": room, "user": sender,
            "created": now, "event_id": event["id"], "asset": choice,
        }
        data = STOCK_MARKET_ASSETS[choice]
        if self._configured_room_language(room) == "en":
            self.send_room_text(room, f"✅ Selected {data['en']} {data['icon']}. Send the investment amount (for example: 100000), or type `{data['en'].lower()} 100000`.")
        else:
            self.send_room_text(room, f"✅ اخترت {data['ar']} {data['icon']}. أرسل مبلغ الاستثمار (مثال: 100000)، أو اكتب `{data['ar']} 100000`.")
        return True

    def _investment_bot_game(self, room, sender):
        if not self._game_cooldown_notice(room, sender, WAGER_GAME_COOLDOWN_SECONDS, "استثمار"):
            return True
        """Free investment game against the bot. No @amount and no image."""
        # Independent fair draw for each round: exactly 50% win / 50% loss.
        # A streak can happen naturally; no hidden loss/win streak adjustment.
        roll = secrets.randbelow(100) + 1
        if roll <= 50:
            reward = secrets.choice((20, 30, 50, 200))
            result = "🏆 فزت!"
        else:
            reward = 0
            result = "❌ لم تفز هذه المرة."
        odds_text = "🎲 نسبة الفوز في كل جولة: 50% — كل جولة مستقلة."
        balance=self._game_award(sender, reward)
        _record_game(sender, "investment", reward, 0)
        self.send_room_text(
            room,
            f"📊✨ استثمار مع البوت\n━━━━━━━━━━━━\n"
            f"👤 اللاعب: @{sender}\n"
            f"{result}\n"
            f"{odds_text}\n"
            f"🎁 المكافأة: +{_fmt_points(reward)} نقطة\n"
            f"💰 الرصيد: {_fmt_points(balance)}"
        )
        if reward > 0:
            self._send_game_winner_card("investment", sender, [room], respect_room_filter=False)
        return True

    def _handle_top_wagerers(self, room, body):
        command = str(body or "").strip().casefold()
        if command not in {"توب المراهنين", "توب رهان"}:
            return False
        data = _game_stats_data()
        totals = []
        for item in data.values():
            if not isinstance(item, dict):
                continue
            games = item.get("games", {})
            if not isinstance(games, dict):
                continue
            total = sum(max(0, int(g.get("staked", 0) or 0))
                        for g in games.values() if isinstance(g, dict))
            if total:
                totals.append((total, str(item.get("username") or "لاعب")))
        totals.sort(key=lambda row: row[0], reverse=True)
        if not totals:
            self.send_room_text(room, "📊 لا توجد سجلات مراهنات حتى الآن.")
            return True
        lines = ["🏆 توب 10 المراهنين في البوت"]
        for index, (amount, name) in enumerate(totals[:10], 1):
            lines.append(f"{index}. @{name} — {_fmt_points(amount)} نقطة")
        self.send_room_text(room, "\n".join(lines))
        return True

    def _horse_game(self, room, sender):
        """Protect the player from the steal game for one minute."""
        now = time.time()
        key = _norm_user(sender)
        self.steal_protection[key] = now + 60.0
        self.send_room_text(
            room,
            f"🐎🛡️ @{sender} حصل على حصانة!\n"
            f"━━━━━━━━━━━━\n"
            f"⏱️ الحماية من السرقة: دقيقة واحدة\n"
            f"🔓 تنتهي بعد 60 ثانية.\n"
            f"━━━━━━━━━━━━"
        )
        return True

    def _steal_game(self, room, sender, requested_victim=""):
        if not self._game_cooldown_notice(room, sender, 40.0, "اسرق"):
            return True

        members = self._room_member_usernames_for_steal(room, sender)
        self.send_room_text(room, f"🕵️ @{sender} جاري البحث عن الضحية...")
        if not members:
            self.send_room_text(room, "❌ فشلت السرقة: لا يوجد عضو آخر متاح للسرقة حالياً.")
            _record_game(sender, "steal", 0, 500)
            self._send_game_winner_card("اسرق_فشل", sender, [room], respect_room_filter=False)
            return True

        # `اسرق` = choose a victim randomly; `اسرق@username` / `اسرق username` = target that member.
        victim = ""
        if requested_victim:
            wanted = _norm_user(requested_victim)
            for candidate in members:
                if _norm_user(candidate) == wanted:
                    victim = candidate
                    break
            if not victim:
                self.send_room_text(room, f"❌ لم أجد @{requested_victim} ضمن أعضاء الغرفة.")
                return True
        else:
            victim = secrets.choice(members)

        victim_balance = _get_points(victim)
        if self._steal_protected(victim):
            self.send_room_text(room, f"@{victim} لديه حصانه من السرقه")
            self._send_game_winner_card("اسرق_فشل", sender, [room], respect_room_filter=False)
            return True
        if victim_balance < 500:
            self.send_room_text(
                room,
                f"🕵️ @{sender} حاول سرقة @{victim}...\n"
                f"❌ فشلت السرقة، المسروق @{victim} مفلس.\n"
                f"💰 رصيده: {_fmt_points(victim_balance)}\n💰 مبلغ الفوز: 0 نقطة\n💸 مبلغ الخسارة: 0 نقطة"
            )
            _record_game(sender, "steal", 0, 500)
            self._send_game_winner_card("اسرق_فشل", sender, [room], respect_room_filter=False)
            return True

        # The result is random each attempt: usually success, sometimes the police catch the thief.
        if secrets.randbelow(100) >= 70:
            self.send_room_text(
                room,
                f"🚨 @{sender} حاول سرقة @{victim}...\n"
                f"🚔 السرقة حرام، تم إبلاغ الشرطة! 😁\n"
                f"❌ لم تتم السرقة.\n💰 مبلغ الفوز: 0 نقطة\n💸 مبلغ الخسارة: 0 نقطة"
            )
            _record_game(sender, "steal", 0, 500)
            self._send_game_winner_card("اسرق_فشل", sender, [room], respect_room_filter=False)
            return True

        transfer = _transfer_points(
            victim, sender, 500, reason="steal", allow_alias_source=True,
            require_exact_target=False, create_target_if_missing=True,
        )
        if not transfer.get("ok"):
            self.send_room_text(room, "⚠️ تعذر اعتماد نتيجة السرقة وحفظ النقاط بأمان؛ لم يتم خصم أو إضافة أي نقاط.")
            self._send_game_winner_card("اسرق_فشل", sender, [room], respect_room_filter=False)
            return True
        thief_balance = transfer["recipient_balance"]
        self.send_room_text(
            room,
            f"🕵️💰 تمت السرقة بنجاح!\n"
            f"👤 السارق: @{sender}\n"
            f"🎯 الضحية: @{victim}\n"
            f"💸 المسروق: 500 نقطة\n"
            f"🎁 @{sender} حصل على +500 نقطة.\n"
            f"💰 رصيد السارق: {_fmt_points(thief_balance)}"
        )
        _record_game(sender, "steal", 500, 500)
        _record_game(victim, "steal", -500, 500)
        self._send_game_winner_card("اسرق_نجاح", sender, [room], respect_room_filter=False)
        return True

    def _table_bot_game(self, room, sender):
        import random
        if not self._game_cooldown_notice(room, sender, 40.0, "طاولة"):
            return True
        player = random.randint(1, 6) + random.randint(1, 6)
        bot = random.randint(1, 6) + random.randint(1, 6)
        if player > bot:
            reward = self._bot_win_reward()
            self._game_award(sender, reward)
            result = f"🎲 طاولة\n👤 أنت: {player}\n🤖 البوت: {bot}\n🏆 فزت بـ {reward} نقطة!\n💰 مبلغ الفوز: +{_fmt_points(reward)} نقطة\n💸 مبلغ الخسارة: 0 نقطة"
        elif player < bot:
            reward = 0
            result = f"🎲 طاولة\n👤 أنت: {player}\n🤖 البوت: {bot}\n🤖 البوت فاز!\n💰 مبلغ الفوز: 0 نقطة\n💸 مبلغ الخسارة: 0 نقطة"
        else:
            reward = 0
            result = f"🎲 طاولة\n👤 أنت: {player}\n🤖 البوت: {bot}\n🤝 تعادل!\n💰 مبلغ الفوز: 0 نقطة\n💸 مبلغ الخسارة: 0 نقطة"
        _record_game(sender, "طاولة", reward, reward)
        self.send_room_text(room, result)
        return True

    def _uno_bot_game(self, room, sender):
        import random
        if not self._game_cooldown_notice(room, sender, 40.0, "اونو"):
            return True
        colors = ["🔴", "🟡", "🟢", "🔵"]
        p_color, p_num = random.choice(colors), random.randint(0, 9)
        b_color, b_num = random.choice(colors), random.randint(0, 9)
        if p_num > b_num:
            reward = self._bot_win_reward()
            self._game_award(sender, reward)
            result = f"🃏 أونو\n👤 أنت: {p_color} {p_num}\n🤖 البوت: {b_color} {b_num}\n🏆 فزت بـ {reward} نقطة!\n💰 مبلغ الفوز: +{_fmt_points(reward)} نقطة\n💸 مبلغ الخسارة: 0 نقطة"
        elif p_num < b_num:
            reward = 0
            result = f"🃏 أونو\n👤 أنت: {p_color} {p_num}\n🤖 البوت: {b_color} {b_num}\n🤖 البوت فاز!\n💰 مبلغ الفوز: 0 نقطة\n💸 مبلغ الخسارة: 0 نقطة"
        else:
            reward = 0
            result = f"🃏 أونو\n👤 أنت: {p_color} {p_num}\n🤖 البوت: {b_color} {b_num}\n🤝 تعادل!\n💰 مبلغ الفوز: 0 نقطة\n💸 مبلغ الخسارة: 0 نقطة"
        _record_game(sender, "اونو", reward, reward)
        self.send_room_text(room, result)
        return True

    def _bot_game_text(self, room, sender, game_key, title, body, reward):
        """Shared helper for the new bot-vs-player text games. No images."""
        if not self._game_cooldown_notice(room, sender, 40.0, game_key):
            return True
        balance = self._game_award(sender, reward)
        _record_game(sender, game_key, reward, 0)
        self.send_room_text(
            room,
            f"{title}\n━━━━━━━━━━━━━━\n{body}\n"
            f"💰 مبلغ الفوز: +{_fmt_points(reward)} نقطة\n"
            f"💸 مبلغ الخسارة: 0 نقطة\n"
            f"💳 رصيدك: {_fmt_points(balance)}\n━━━━━━━━━━━━━━"
        )
        return True

    def _coin_bot_game(self, room, sender, choice=""):
        if not self._game_cooldown_notice(room, sender, 40.0, "عملة"):
            return True
        choice = str(choice or "").strip()
        key = _game_choice_key(room, sender)
        if choice in ("وجه", "كتابة"):
            result = secrets.choice(("وجه", "كتابة"))
            won = choice == result
            reward = self._bot_win_reward() if won else 0
            balance = self._game_award(sender, reward)
            _record_game(sender, "coin", reward, 0)
            self.send_room_text(room, f"🪙 لعبة العملة\n━━━━━━━━━━━━━━\n@{sender}\n🎯 اختيارك: {choice}\n🪙 النتيجة: {result}\n{('🏆 فزت!' if won else '❌ لم تفز هذه المرة.')}\n🎁 +{_fmt_points(reward)} نقطة\n💰 رصيدك: {_fmt_points(balance)}")
            return True
        self.pending_bot_choices[key] = {"game": "coin", "created": time.time(), "result": secrets.choice(("وجه", "كتابة"))}
        self.send_room_text(room, f"🪙 لعبة العملة\n━━━━━━━━━━━━━━\n@{sender}\n\u20661.\u2069 وجه\n\u20662.\u2069 كتابة\n\n📌 أرسل الرقم فقط")
        return True

    def _wheel_bot_game(self, room, sender):
        if not self._game_cooldown_notice(room, sender, 40.0, "عجلة"):
            return True
        reward = 0 if secrets.randbelow(5) == 0 else self._bot_win_reward()
        balance = self._game_award(sender, reward)
        _record_game(sender, "wheel", reward, 0)
        self.send_room_text(
            room,
            f"🎡 عجلة الحظ\n━━━━━━━━━━━━━━\n@{sender}\n"
            f"🎯 دارت العجلة وتوقفت على: {_fmt_points(reward)} نقطة\n"
            f"💰 مبلغ الفوز: +{_fmt_points(reward)} نقطة\n"
            f"💸 مبلغ الخسارة: 0 نقطة\n"
            f"💳 رصيدك: {_fmt_points(balance)}\n━━━━━━━━━━━━━━"
        )
        return True

    def _box_bot_game(self, room, sender, raw):
        raw = str(raw or "").strip()
        m = re.fullmatch(r"صندوق[@ ]([1-3])", raw, re.I)
        if not self._game_cooldown_notice(room, sender, 40.0, "صندوق"):
            return True
        key = _game_choice_key(room, sender)
        chosen = int(m.group(1)) if m else None
        if chosen is None:
            self.pending_bot_choices[key] = {
                "game": "box", "created": time.time(),
                "prize_box": secrets.randbelow(3) + 1,
                "reward": self._bot_win_reward(),
            }
            self.send_room_text(room, f"📦 لعبة الصناديق\n━━━━━━━━━━━━━━\n@{sender}\n\u20661.\u2069 صندوق 1\n\u20662.\u2069 صندوق 2\n\u20663.\u2069 صندوق 3\n\n📌 أرسل الرقم فقط")
            return True
        prize_box = secrets.randbelow(3) + 1
        reward = self._bot_win_reward() if chosen == prize_box else 0
        body = (f"📦 اخترت الصندوق {chosen}\n🏆 الصندوق الرابح: {prize_box}\n✅ ربحت!" if reward else f"📦 اخترت الصندوق {chosen}\n🎲 الصندوق الرابح كان: {prize_box}\n❌ لم تربح.")
        balance = self._game_award(sender, reward)
        _record_game(sender, "box", reward, 0)
        self.send_room_text(room, f"📦 لعبة الصناديق\n━━━━━━━━━━━━━━\n@{sender}\n{body}\n🎁 +{_fmt_points(reward)} نقطة\n💰 رصيدك: {_fmt_points(balance)}")
        return True

    def _cup_bot_game(self, room, sender, raw):
        m = re.fullmatch(r"(?:كوب|كأس)[@ ]([1-3])", str(raw or "").strip(), re.I)
        if not self._game_cooldown_notice(room, sender, 40.0, "كوب"):
            return True
        chosen = int(m.group(1)) if m else None
        hidden = secrets.randbelow(3) + 1
        reward = self._bot_win_reward() if chosen == hidden else 0
        if chosen is None:
            body = "🥤 اختر الكوب: كوب@1 أو كوب@2 أو كوب@3"
        elif reward:
            body = f"🥤 اخترت الكوب {chosen}\n🏆 الكأس الصحيح: {hidden}\n✅ وجدت الجائزة!"
        else:
            body = f"🥤 اخترت الكوب {chosen}\n🎲 الجائزة كانت في الكوب: {hidden}\n❌ الكوب الخطأ."
        balance = self._game_award(sender, reward)
        _record_game(sender, "cup", reward, 0)
        self.send_room_text(room, f"🥤 لعبة الكؤوس\n━━━━━━━━━━━━━━\n@{sender}\n{body}\n🎁 +{_fmt_points(reward)} نقطة\n💰 الرصيد: {_fmt_points(balance)}")
        return True

    def _monster_bot_game(self, room, sender):
        if not self._game_cooldown_notice(room, sender, 40.0, "وحش"):
            return True
        player = secrets.randbelow(6) + 1
        monster = secrets.randbelow(6) + 1
        if player > monster:
            reward = self._bot_win_reward()
            result = "⚔️ هزمت الوحش!"
        elif player < monster:
            reward = 0
            result = "💀 الوحش هزمك."
        else:
            reward = 0
            result = "🤝 تعادل مع الوحش — لا توجد جائزة."
        balance = self._game_award(sender, reward)
        _record_game(sender, "monster", reward, 0)
        self.send_room_text(room, f"👹 معركة الوحش\n━━━━━━━━━━━━━━\n👤 قوتك: {player}\n👹 قوة الوحش: {monster}\n{result}\n🎁 +{_fmt_points(reward)} نقطة\n💰 الرصيد: {_fmt_points(balance)}")
        return True

    def _volcano_bot_game(self, room, sender):
        if not self._game_cooldown_notice(room, sender, 40.0, "بركان"):
            return True
        result = secrets.randbelow(5)
        reward = 0 if result == 0 else self._bot_win_reward()
        outcome = "🌋 خرجت الجائزة من البركان!" if reward else "🌋 انفجر البركان ولم تجد جائزة."
        balance = self._game_award(sender, reward)
        _record_game(sender, "volcano", reward, 0)
        self.send_room_text(room, f"🌋 لعبة البركان\n━━━━━━━━━━━━━━\n@{sender}\n{outcome}\n🎁 +{_fmt_points(reward)} نقطة\n💰 الرصيد: {_fmt_points(balance)}")
        return True

    def _bird_bot_game(self, room, sender):
        if not self._game_cooldown_notice(room, sender, 40.0, "طائر"):
            return True
        birds = [("🐦 عصفور", self._bot_win_reward()), ("🦅 نسر", self._bot_win_reward()), ("🦉 بومة", self._bot_win_reward()), ("🦜 ببغاء", self._bot_win_reward()), ("🌫️ لم يظهر طائر", 0)]
        bird, reward = secrets.choice(birds)
        balance = self._game_award(sender, reward)
        _record_game(sender, "bird", reward, 0)
        self.send_room_text(room, f"🪶 صيد الطائر\n━━━━━━━━━━━━━━\n@{sender}\n🎯 ظهر: {bird}\n🎁 +{_fmt_points(reward)} نقطة\n💰 الرصيد: {_fmt_points(balance)}")
        return True

    def _star_bot_game(self, room, sender):
        if not self._game_cooldown_notice(room, sender, 40.0, "نجم"):
            return True
        stars = [("⭐ عادية", self._bot_win_reward()), ("🌟 لامعة", self._bot_win_reward()), ("💫 نادرة", self._bot_win_reward()), ("✨ أسطورية", self._bot_win_reward()), ("🌑 لم تلتقط نجماً", 0)]
        star, reward = secrets.choice(stars)
        balance = self._game_award(sender, reward)
        _record_game(sender, "star", reward, 0)
        self.send_room_text(room, f"⭐ لعبة النجمة\n━━━━━━━━━━━━━━\n@{sender}\n🎯 حظك: {star}\n🎁 +{_fmt_points(reward)} نقطة\n💰 الرصيد: {_fmt_points(balance)}")
        return True

    def _million_bank_game(self, room, sender_name):
        # The cooldown check itself emits the exact remaining wait time.
        # For a fresh play, send the acknowledgement immediately from the
        # worker thread before doing any reveal delay.
        if not self._game_cooldown_notice(room, sender_name, MILLION_BANK_COOLDOWN_SECONDS, "بنك مليون"):
            return True
        english = self._configured_room_language(room) == "en"
        if english:
            self.send_room_text(room, f"🏦\n{sender_name}\nSearching for a million\n🏦")
        else:
            self.send_room_text(room, f"🏦\n{sender_name}\nجاري البحث عن مليون\n🏦")
        won = _money_game_roll("million")
        reward = 1_000_000 if won else 0
        _record_game(sender_name, "million", reward, 0)
        if not won:
            loss_text = (f"🏦\n{sender_name}\nDid not get the million\n🏦"
                         if english else f"🏦\n{sender_name}\nلم يتم الحصول على مليون\n🏦")
            # محاولة الحصول على المليون تبقى داخل الغرفة التي أُرسلت منها.
            self.send_room_text(room, loss_text)
            return True
        _record_money_game_win("million", sender_name)
        self._game_award(sender_name, reward)
        winner_photo = self.user_photos.get(_norm_user(sender_name), "") or self._lookup_profile_photo(sender_name)

        # IMPORTANT: for the bank-win broadcast use only rooms confirmed as
        # connected in THIS WebSocket session. ``_active_rooms()`` also contains
        # historical/persisted rooms and can make the bot send to stale rooms.
        connected = {str(r).strip() for r in getattr(self, "connected_rooms", set()) if str(r).strip()}
        preferred_room = str(room or "").strip()
        target_rooms = ([preferred_room] if preferred_room else []) + sorted(
            (target for target in connected if _norm_room(target) != _norm_room(preferred_room)),
            key=str.casefold,
        )
        if not target_rooms:
            target_rooms = [preferred_room]

        # Do not burst all room packets at once. The short pacing keeps the
        # realtime connection alive while preserving the existing all-room
        # winner announcement and image behaviour.
        for index, target_room in enumerate(target_rooms):
            try:
                language = self._configured_room_language(target_room)
                self.send_room_text(target_room, _money_game_win_text("million", sender_name, language))
            except Exception as exc:
                self.log("[GAME] bank winner text send failed:", target_room, repr(exc))
            if index + 1 < len(target_rooms):
                time.sleep(0.05)

        self._send_game_winner_card("بنك مليون", sender_name, target_rooms, send_delay=0.35)
        return True

    def _run_game_command_async(self, room, text, sender_name):
        """Run game logic outside the WebSocket receive callback.

        Some legacy games intentionally use time.sleep() for their reveal
        animation. Keeping them off the receive thread means a following NS
        command is received and handled immediately instead of waiting for the
        game to finish sleeping.
        """
        # handle_game_command() is intentionally permissive for callers that
        # already routed a command, but this receive-path helper must not
        # claim ordinary conversation.  Returning True for every message
        # used to prevent automatic replies such as "أنا" and "بوت" from
        # ever reaching the reply router.
        if not _looks_like_bot_command(text):
            raw = str(text or "").strip()
            pending_key = _game_choice_key(room, sender_name)
            has_pending_bomb = pending_key in getattr(self, "_pending_bomb_targets", {})
            ludo_game = getattr(self, "ludo_games", {}).get(f"ludo:{_norm_room(room)}")
            has_pending_choice = pending_key in getattr(self, "pending_bot_choices", {})
            has_pending_stock = (
                pending_key in getattr(self, "stock_pending", {})
                or pending_key in getattr(self, "_stock_auto_gold_pending", {})
            )
            is_ludo_choice = raw in {"1", "2", "3", "4", "١", "٢", "٣", "٤"} and bool(ludo_game)
            ascii_digits = raw.translate(str.maketrans(
                "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"
            ))
            is_bare_wager_stake = bool(re.fullmatch(
                r"[0-9][0-9,]*(?:\.[0-9]+)?[kmb]?", ascii_digits, re.I
            ))
            wager_states = getattr(self, "wager_waiting", {})
            has_pending_wager = is_bare_wager_stake and any(
                isinstance(value, dict) and not value.get("_refund_pending")
                for value in (wager_states.values() if isinstance(wager_states, dict) else [])
            )
            if not (has_pending_bomb or has_pending_choice or has_pending_stock or is_ludo_choice or has_pending_wager):
                return False
        response_language = self._capture_room_response_language(room, text)
        def worker():
            try:
                self.handle_game_command(room, text, sender_name)
            except Exception as exc:
                self.log("[GAME] async handler failed:", repr(exc))
                if isinstance(exc, PointsInsufficientError):
                    try:
                        self.send_room_text(room, "❌ تغيّر رصيدك قبل اعتماد الرهان؛ لم تُقبل العملية. تحقق من رصيدك ثم أعد المحاولة.")
                    except Exception:
                        pass
                    return
                if isinstance(exc, PointsPersistenceError):
                    try:
                        self.send_room_text(room, "⚠️ تعذر حفظ تحديث النقاط في قاعدة البيانات؛ لم أعتمد مكسباً جديداً. يرجى التحقق من رصيدك قبل إعادة اللعب.")
                    except Exception:
                        pass
                    return
                try:
                    self.report_master_error("الألعاب", exc, room)
                except Exception:
                    pass
        if not getattr(self, "_replaying_bot_action", False):
            self._remember_bot_action(room, text, sender_name, is_private=False)
        threading.Thread(
            target=self._localized_room_worker(room, response_language, worker),
            name="game-command",
            daemon=True,
        ).start()
        return True

    def _route_active_board_game_join(self, room, sender_name, text):
        """Give an active Ludo/Snake join request priority over cricket's Join."""
        command = " ".join(_normalize_game_command_text(text).strip().casefold().replace("ة", "ه").split())
        board_aliases = {
            "join", "انضمام", "join ludo", "join snake", "join snake game", "join ludo game",
            "انضمام ثعبان", "انضمام لعبة ثعبان", "انضمام لعبه ثعبان",
            "انضمام لودو", "انضمام لعبة لودو", "انضمام لعبه لودو",
        }
        if command not in board_aliases:
            return False
        target_snake = "ثعبان" in command or "snake" in command
        target_ludo = "لودو" in command or "ludo" in command
        room_key = _norm_room(room)
        ludo = getattr(self, "ludo_games", {}).get(f"ludo:{room_key}")
        snake = getattr(self, "snake_games", {}).get(f"snake:{room_key}")
        if target_snake and not isinstance(snake, dict):
            self.send_room_text(room, "⏳ لا توجد لعبة ثعبان نشطة في هذه الغرفة الآن.")
            return True
        if target_ludo and not isinstance(ludo, dict):
            self.send_room_text(room, "⏳ لا توجد لعبة لودو نشطة في هذه الغرفة الآن.")
            return True
        if target_snake and isinstance(snake, dict):
            if not _is_verified_user(sender_name):
                self.send_room_text(room, f"🔒 @{sender_name} حسابك غير موثق لاستخدام الألعاب.\n{_verification_notice()}")
                return True
            return self._run_game_command_async(room, "انضمام لعبة ثعبان", sender_name)
        if target_ludo and isinstance(ludo, dict):
            if not _is_verified_user(sender_name):
                self.send_room_text(room, f"🔒 @{sender_name} حسابك غير موثق لاستخدام الألعاب.\n{_verification_notice()}")
                return True
            return self._run_game_command_async(room, "انضمام لعبة لودو", sender_name)
        if not isinstance(ludo, dict) and not isinstance(snake, dict):
            return False
        if not _is_verified_user(sender_name):
            return False
        return self._run_game_command_async(room, "join", sender_name)

    def _game_public_image(self, path, route="games"):
        base=_public_base_url()
        if not base: return ""
        path.parent.mkdir(parents=True,exist_ok=True)
        try: self._verify_public_media_url(f"{base}/{route}/{path.name}","image")
        except Exception: pass
        return f"{base}/{route}/{path.name}"

    def _game_rooms(self, game):
        rooms = game.get("rooms", set()) if isinstance(game, dict) else set()
        if not isinstance(rooms, set):
            rooms = set(rooms or [])
        return set(rooms)

    def _broadcast_game_start(self, title, game):
        for r in self._active_rooms():
            try:
                self.send_room_text(r, title)
            except Exception:
                pass

    def _broadcast_game_result_all_rooms(self, text, image_path=None, winner_photo_url=""):
        url = self._game_public_image(image_path) if image_path else ""
        sent = 0
        for r in self._active_rooms():
            try:
                if winner_photo_url and not _game_results_disabled_for_room(r):
                    self.send_room_media(r, winner_photo_url, "image")
                if url:
                    if not _game_results_disabled_for_room(r):
                        self.send_room_media(r, url, "image")
                self.send_room_text(r, text)
                sent += 1
            except Exception:
                pass
        return sent

    def _game_square_avatar(self, username, size=58):
        """Fetch a profile image as a true square board piece, not a circle."""
        if not PIL_AVAILABLE:
            return None
        username = str(username or "").strip().lstrip("@")
        if not username or username == "🤖 البوت":
            return None
        try:
            photo = self.user_photos.get(username.casefold(), "") or self._lookup_profile_photo(username)
            av = _download_profile_photo(photo)
            return _fit_crop(av, (size, size)).convert("RGB") if av is not None else None
        except Exception:
            return None

    def _render_snake_board(self, state, winner_name=""):
        if not PIL_AVAILABLE: return None
        from PIL import Image, ImageDraw, ImageFont
        W=1000; H=1160; cell=92; left=40; top=130
        img=Image.new("RGB",(W,H),(8,15,25)); d=ImageDraw.Draw(img)
        font=_gift_font("1",24); small=_gift_font("1",18); title_font=_gift_font("1",34)
        d.rounded_rectangle((20,18,W-20,110), radius=22, fill=(20,35,52), outline=(245,198,70), width=3)
        if winner_name:
            # Final result: put the winner's name and avatar INSIDE the final board image.
            winner_photo = ""
            try:
                winner_photo = self.user_photos.get(str(winner_name).casefold(), "") or self._lookup_profile_photo(winner_name)
            except Exception:
                pass
            avatar = _load_sender_avatar(winner_photo, 64) if winner_photo else None
            if avatar is not None:
                _paste_avatar_safe(img, avatar, (36, 41))
            d.text((W//2,38), "🏆 الفائز" if state.get("lang")!="en" else "🏆 WINNER",
                   fill=(255,215,80), font=small, anchor="ma")
            _draw_name_centered(
                d, (W//2, 80), str(winner_name).lstrip('@'), 34,
                (255,255,255), W - 180, apply_correction=True,
            )
        else:
            d.text((W//2,38), "السلم والثعبان" if state.get("lang")!="en" else "SNAKE & LADDERS", fill=(255,215,80), font=title_font, anchor="ma")
            roll=state.get("last_roll")
            if roll is not None:
                d.text((W//2,82), f"الرول: {roll}" if state.get("lang")!="en" else f"ROLL: {roll}", fill=(235,245,255), font=small, anchor="ma")

        def cell_center(pos):
            pos=max(1,min(100,int(pos)))
            idx=pos-1; row=idx//10; col=idx%10
            if row%2: col=9-col
            return (left+col*cell+cell//2, top+(9-row)*cell+cell//2)

        # board cells
        for n in range(1,101):
            x,y=cell_center(n)
            x0=x-cell//2; y0=y-cell//2
            fill=(20,34,48) if n%2 else (24,43,59)
            d.rectangle((x0,y0,x0+cell,y0+cell), fill=fill, outline=(75,125,155), width=2)
            d.text((x0+8,y0+6),str(n),fill=(235,240,245),font=font)

        # Realistic-looking ladders: rails + rungs
        ladders={3:22,8:30,28:55,36:44,51:72,71:92,80:99}
        for a,b in ladders.items():
            x1,y1=cell_center(a); x2,y2=cell_center(b)
            dx=x2-x1; dy=y2-y1; ln=max(1,(dx*dx+dy*dy)**0.5); nx=-dy/ln; ny=dx/ln
            rail=12
            for off in (-12,12):
                d.line((x1+nx*off,y1+ny*off,x2+nx*off,y2+ny*off),fill=(242,190,45),width=7)
            steps=max(3,int(ln//28))
            for k in range(1,steps):
                t=k/steps; cx=x1+dx*t; cy=y1+dy*t
                d.line((cx+nx*18,cy+ny*18,cx-nx*18,cy-ny*18),fill=(255,225,100),width=6)

        # Realistic-looking snakes: curved segmented body, head and eyes
        snakes={98:40,95:75,92:70,88:48,62:18,48:26,24:5,17:7}
        for a,b in snakes.items():
            x1,y1=cell_center(a); x2,y2=cell_center(b)
            dx=x2-x1; dy=y2-y1; ln=max(1,(dx*dx+dy*dy)**0.5); px=-dy/ln; py=dx/ln
            pts=[]
            for k in range(17):
                t=k/16; cx=x1+dx*t; cy=y1+dy*t; wave=((k%4)-1.5)*9
                pts.append((cx+px*wave,cy+py*wave))
            d.line(pts,fill=(35,190,105),width=20,joint="curve")
            d.line(pts,fill=(20,120,70),width=11,joint="curve")
            hx,hy=pts[0]; d.ellipse((hx-17,hy-17,hx+17,hy+17),fill=(40,205,110),outline=(10,70,40),width=3)
            d.ellipse((hx-8,hy-5,hx-3,hy),fill=(255,255,255)); d.ellipse((hx+3,hy-5,hx+8,hy),fill=(255,255,255))
            d.ellipse((hx-6,hy-4,hx-4,hy-2),fill=(0,0,0)); d.ellipse((hx+4,hy-4,hx+6,hy-2),fill=(0,0,0))
            d.line((hx,hy+12,hx+18,hy+12),fill=(230,60,70),width=3)

        # Player profile photos are the pieces; no red circles.
        colors=[(255,210,70),(80,190,255),(220,90,220),(80,225,130)]
        for i,(u,pos) in enumerate(state.get("positions",{}).items()):
            cx,cy=cell_center(pos); photo=""
            try:
                photo=self.user_photos.get(str(u).casefold(), "") or self._lookup_profile_photo(u)
            except Exception:
                photo=""
            avatar=_load_sender_avatar(photo, 58) if photo else None
            if avatar is not None:
                _paste_avatar_safe(img, avatar, (cx-29,cy-29))
            else:
                d.rounded_rectangle((cx-27,cy-27,cx+27,cy+27),radius=12,fill=colors[i%len(colors)],outline=(255,255,255),width=2)
                d.text((cx,cy),str(i+1),fill=(10,10,10),font=small,anchor="mm")
            # Keep the piece clean; player names are rendered in the
            # dedicated legend below the board so long Arabic/Latin names
            # remain readable and are never clipped by a board cell.

        # Clear player-name panel below the board.  The old 14px text was
        # too small and was often hidden/overlapped by pieces.
        panel_y0 = top + 10*cell + 10
        panel_y1 = H - 12
        d.rounded_rectangle((20, panel_y0, W-20, panel_y1), radius=18,
                            fill=(16,30,45), outline=(245,198,70), width=3)
        players = list(state.get("positions", {}).items())
        if players:
            d.text((W//2, panel_y0 + 18), "👤 اللاعبون",
                   fill=(245,205,80), font=_gift_font("1",22), anchor="ma")
            col_w = (W-50) / max(1, len(players))
            for i,(u,_pos) in enumerate(players[:4]):
                center_x = int(25 + col_w*i + col_w/2)
                avatar = None
                try:
                    photo = self.user_photos.get(str(u).casefold(), "") or self._lookup_profile_photo(u)
                    avatar = _load_sender_avatar(photo, 42) if photo else None
                except Exception:
                    avatar = None
                if avatar is not None:
                    _paste_avatar_safe(img, avatar, (center_x-21, panel_y0+42))
                else:
                    d.ellipse((center_x-21, panel_y0+42, center_x+21, panel_y0+84),
                              fill=colors[i%len(colors)], outline=(255,255,255), width=2)
                    d.text((center_x, panel_y0+63), str(i+1), fill=(10,10,10),
                           font=_gift_font("1",16), anchor="mm")
                # Actual mixed-font measurement keeps Arabic names centered and
                # automatically reduces the font size for long usernames.
                max_name_w = max(70, int(col_w-16))
                _draw_name_centered(
                    d, (center_x, panel_y0+91),
                    "@" + str(u).lstrip("@"),
                    22, (255,255,255), max_name_w
                )

        out=BASE_DIR/"generated_games"/f"snake_{uuid.uuid4().hex}.jpg"; out.parent.mkdir(parents=True,exist_ok=True)
        img.save(out,"JPEG",quality=84,optimize=True); return out

    def _game_roll_command(self, raw):
        """Normalize the roll command so common spellings are accepted reliably."""
        value = re.sub(r"\s+", "", str(raw or "").strip().casefold())
        return value in {"rool", "roll", "رول", "رول!", "rool!", "roll!"}

    def _send_game_cover(self, game_key, game):
        """Send the fixed game cover once when a new game starts."""
        try:
            filename = GAME_IMAGE_FILES.get(game_key, "")
            path = ASSETS_DIR / filename
            if not path.is_file():
                return
            url = self._game_public_image(path, route="assets")
            if not url:
                return
            # The opening cover is part of the game start, so it is announced
            # to every active room just like the start message.
            for r in self._active_rooms():
                if _game_results_disabled_for_room(r):
                    continue
                self.send_room_media(r, url, "image")
        except Exception as exc:
            self.log("[GAME] cover send failed:", repr(exc))

    def _render_snake_board(self, state, winner_name=""):
        if not PIL_AVAILABLE: return None
        from PIL import Image, ImageDraw
        W=1000; H=1180; cell=92; left=40; top=130
        img=Image.new("RGB",(W,H),(8,15,25)); d=ImageDraw.Draw(img)
        font=_gift_font("1",24); small=_gift_font("1",18); title_font=_gift_font("1",34)
        d.rounded_rectangle((20,18,W-20,110), radius=22, fill=(20,35,52), outline=(245,198,70), width=3)
        d.text((W//2,38), "السلم والثعبان" if state.get("lang")!="en" else "SNAKE & LADDERS", fill=(255,215,80), font=title_font, anchor="ma")
        roll=state.get("last_roll")
        if roll is not None:
            d.text((W//2,82), f"الرول: {roll}" if state.get("lang")!="en" else f"ROLL: {roll}", fill=(235,245,255), font=small, anchor="ma")

        def cell_center(pos):
            pos=max(1,min(100,int(pos)))
            idx=pos-1; row=idx//10; col=idx%10
            if row%2: col=9-col
            return (left+col*cell+cell//2, top+(9-row)*cell+cell//2)

        for n in range(1,101):
            x,y=cell_center(n); x0=x-cell//2; y0=y-cell//2
            fill=(20,34,48) if n%2 else (24,43,59)
            d.rectangle((x0,y0,x0+cell,y0+cell), fill=fill, outline=(75,125,155), width=2)
            d.text((x0+8,y0+6),str(n),fill=(235,240,245),font=font)

        ladders={3:22,8:30,28:55,36:44,51:72,71:92,80:99}
        for a,b in ladders.items():
            x1,y1=cell_center(a); x2,y2=cell_center(b)
            dx=x2-x1; dy=y2-y1; ln=max(1,(dx*dx+dy*dy)**0.5); nx=-dy/ln; ny=dx/ln
            for off in (-12,12):
                d.line((x1+nx*off,y1+ny*off,x2+nx*off,y2+ny*off),fill=(242,190,45),width=7)
            for k in range(1,max(3,int(ln//28))):
                t=k/max(3,int(ln//28)); cx=x1+dx*t; cy=y1+dy*t
                d.line((cx+nx*18,cy+ny*18,cx-nx*18,cy-ny*18),fill=(255,225,100),width=6)

        # Snakes: shaded, scale-textured and dimensional instead of cartoon-green lines.
        snakes={98:40,95:75,92:70,88:48,62:18,48:26,24:5,17:7}
        snake_palettes=[((38,95,48),(12,35,18)),((92,66,32),(35,20,10)),((55,80,115),(18,25,45)),((105,48,48),(45,15,15))]
        for si,(a,b) in enumerate(snakes.items()):
            x1,y1=cell_center(a); x2,y2=cell_center(b)
            dx=x2-x1; dy=y2-y1; ln=max(1,(dx*dx+dy*dy)**0.5); px=-dy/ln; py=dx/ln
            pts=[]
            for k in range(49):
                t=k/48.0; cx=x1+dx*t; cy=y1+dy*t
                wave=math.sin(t*math.pi*4 + si*0.7)*11
                pts.append((cx+px*wave,cy+py*wave))
            base,shadow=snake_palettes[si % len(snake_palettes)]
            # soft shadow under the body
            shadow_pts=[(x+4,y+5) for x,y in pts]
            d.line(shadow_pts,fill=(3,8,10),width=27,joint="curve")
            d.line(pts,fill=shadow,width=25,joint="curve")
            d.line(pts,fill=base,width=20,joint="curve")
            # Highlight strip and repeated scale marks give a photographic texture.
            hi=tuple(min(255,c+42) for c in base)
            d.line([(x+px*3,y+py*3) for x,y in pts],fill=hi,width=5,joint="curve")
            for k in range(4,47,3):
                x,y=pts[k]
                for side in (-1,1):
                    sx=x+px*side*5; sy=y+py*side*5
                    d.arc((sx-4,sy-3,sx+4,sy+3),20,160,fill=tuple(min(255,c+65) for c in base),width=1)
            # Head at the high-number end, with eye, nostril and forked tongue.
            hx,hy=pts[0]; tx,ty=pts[2]; ux,uy=(hx-tx,hy-ty); ulen=max(1,(ux*ux+uy*uy)**0.5); ux/=ulen; uy/=ulen
            vx,vy=-uy,ux
            head_center=(hx+ux*5,hy+uy*5)
            d.ellipse((head_center[0]-18,head_center[1]-14,head_center[0]+18,head_center[1]+14),fill=tuple(min(255,c+12) for c in base),outline=(8,12,8),width=2)
            for side in (-1,1):
                ex=head_center[0]+ux*7+vx*side*7; ey=head_center[1]+uy*7+vy*side*7
                d.ellipse((ex-4,ey-4,ex+4,ey+4),fill=(235,220,80),outline=(10,10,10),width=1)
                d.ellipse((ex-1.5,ey-2,ex+1.5,ey+2),fill=(5,5,5))
            # forked tongue
            tongue_start=(head_center[0]+ux*17,head_center[1]+uy*17)
            tongue_mid=(tongue_start[0]+ux*13,tongue_start[1]+uy*13)
            d.line((tongue_start[0],tongue_start[1],tongue_mid[0],tongue_mid[1]),fill=(190,35,45),width=2)
            d.line((tongue_mid[0],tongue_mid[1],tongue_mid[0]+vx*6+ux*8,tongue_mid[1]+vy*6+uy*8),fill=(190,35,45),width=2)
            d.line((tongue_mid[0],tongue_mid[1],tongue_mid[0]-vx*6+ux*8,tongue_mid[1]-vy*6+uy*8),fill=(190,35,45),width=2)

        colors=[(255,210,70),(80,190,255),(220,90,220),(80,225,130)]
        for i,(u,pos) in enumerate(state.get("positions",{}).items()):
            cx,cy=cell_center(pos); photo=""
            try: photo=self.user_photos.get(str(u).casefold(), "") or self._lookup_profile_photo(u)
            except Exception: pass
            avatar=_load_sender_avatar(photo, 58) if photo else None
            if avatar is not None: _paste_avatar_safe(img, avatar, (cx-29,cy-29))
            else:
                d.rounded_rectangle((cx-27,cy-27,cx+27,cy+27),radius=12,fill=colors[i%len(colors)],outline=(255,255,255),width=2)
                d.text((cx,cy),str(i+1),fill=(10,10,10),font=small,anchor="mm")
            d.text((cx,cy+31),str(u)[:12],fill=(255,255,255),font=_gift_font("1",14),anchor="ma")

        # Final winner card: use the same bottom result-panel style as Ludo.
        if winner_name:
            panel_y0 = top + 10*cell + 10
            panel_y1 = min(H-12, panel_y0 + 100)
            d.rounded_rectangle((left, panel_y0, left+10*cell, panel_y1),
                                radius=18, fill=(16,30,45), outline=(245,198,70), width=3)
            winner_photo = ""
            try:
                winner_photo = self.user_photos.get(str(winner_name).casefold(), "") or self._lookup_profile_photo(winner_name)
            except Exception:
                pass
            avatar = _load_sender_avatar(winner_photo, 72) if winner_photo else None
            if avatar is not None:
                _paste_avatar_safe(img, avatar, (left+20, panel_y0+14))
            d.text((W//2, panel_y0+23), "🏆 الفائز" if state.get("lang")!="en" else "🏆 WINNER",
                   fill=(245,205,80), font=_gift_font("1",18), anchor="ma")
            _draw_name_centered(
                d, (W//2, panel_y0+80), str(winner_name).lstrip('@'), 24,
                (255,255,255), W - 180, apply_correction=True,
            )

        out=BASE_DIR/"generated_games"/f"snake_{uuid.uuid4().hex}.jpg"; out.parent.mkdir(parents=True,exist_ok=True)
        img.save(out,"JPEG",quality=86,optimize=True); return out

    def _schedule_board_game_timeout(self, game_key, game, label):
        """Cancel Ludo/Snake after 2 minutes without any player interaction."""
        old_timer = game.get("timeout_timer")
        try:
            if old_timer:
                old_timer.cancel()
        except Exception:
            pass
        game["last_activity_at"] = time.time()
        def expire():
            is_snake = str(game_key).startswith("snake:")
            current = self.snake_games.get(game_key) if is_snake else self.ludo_games.get(game_key)
            if current is not game:
                return
            last = float(game.get("last_activity_at", 0) or 0)
            if time.time() - last < 120:
                self._schedule_board_game_timeout(game_key, game, label)
                return
            rooms = self._game_rooms(game) or [str(game.get("origin_room") or "")]
            for r in rooms:
                if r:
                    self.send_room_text(r, f"⌛ انتهت لعبة {label} تلقائيًا لعدم وجود تفاعل لمدة دقيقتين.")
            if game_key == "__shared_snake__":
                self.snake_games.pop(game_key, None)
            else:
                self.ludo_games.pop(game_key, None)
        timer = threading.Timer(120.0, expire)
        timer.daemon = True
        game["timeout_timer"] = timer
        timer.start()

    def _snake_command(self,room,sender,raw):
        key=f"snake:{_norm_room(room)}"; low=str(raw or "").strip().casefold(); english=(self._effective_room_language(room) == "en" or low in ("snake","snakes and ladders","سناكي"))
        game=self.snake_games.get(key)
        if low in ("ثعبان","snake","سناكي") and not game:
            if not self._board_game_cooldown_notice(room, sender, "snake"):
                return True
            game={"players":[sender],"positions":{sender:1},"lang":"en" if english else "ar","turn":0,"created":time.time(),"rooms":{room},"origin_room":room,"last_roll":None,"last_roll_at":0.0}
            self.snake_games[key]=game; self._schedule_board_game_timeout(key, game, "السلم والثعبان"); self._send_game_cover("snake_ladders",game)
            self._broadcast_game_start("🐍 بدأت لعبة السلم والثعبان! جاري البحث عن خصم. للمشاركة اكتب join" if not english else "🐍 Snake & Ladders started! Waiting for an opponent. Type join to participate.",game)
            return True
        if not game: return False
        origin_room = str(game.get("origin_room") or next(iter(game.get("rooms", {room})), room))
        game["origin_room"] = origin_room
        # The game itself is local to its origin room. However, JOIN is global:
        # if someone sends join from another room while this game is active,
        # tell them a game is already running instead of silently ignoring it.
        if room != origin_room:
            if low in ("join", "انضمام"):
                self.send_room_text(room, f"🎮 لديك لعبة السلم والثعبان شغالة بالفعل في غرفة: {origin_room}\n✏️ اكتب اسم اللعبة لبدء لعبة جديدة.")
            return True
        game.setdefault("rooms",set()).add(origin_room)
        if low in ("ثعبان","snake","سناكي"):
            self.send_room_text(room, f"🐍 توجد لعبة السلم والثعبان شغالة بالفعل في غرفة: {origin_room}. اكتب join للمشاركة." if game.get("lang")!="en" else f"🐍 A Snake & Ladders game is already running in room: {origin_room}. Type join to join."); return True
        if low in ("join","انضمام"):
            if sender not in game["players"] and len(game["players"])<2:
                game["players"].append(sender); game["positions"][sender]=1; game["rooms"].add(room); self._schedule_board_game_timeout(key, game, "السلم والثعبان")
                self.send_room_text(room,"🐍 تم انضمام اللاعب. اكتب rool للعب.")
            return True
        if self._game_roll_command(raw) and sender in game["players"]:
            if len(game.get("players",[])) < 2:
                self.send_room_text(room,"⏳ اللعبة ما زالت تنتظر لاعباً ثانياً.\n👥 اكتب join أولاً، وبعدها يبدأ الرول.")
                return True
            idx=game["players"].index(sender)
            if idx != game.get("turn",0): self.send_room_text(room,"⏳ انتظر دورك."); return True
            now=time.monotonic()
            if now-float(game.get("last_roll_at",0.0) or 0.0)<0.45: return True
            game["last_roll_at"]=now; game["rooms"].add(room); self._schedule_board_game_timeout(key, game, "السلم والثعبان")
            roll=secrets.randbelow(6)+1; old_pos=game["positions"].get(sender,1); raw_pos=min(100,old_pos+roll)
            ladders={3:22,8:30,28:55,36:44,51:72,71:92,80:99}; snakes={98:40,95:75,92:70,88:48,62:18,48:26,24:5,17:7}
            final=ladders.get(raw_pos,snakes.get(raw_pos,raw_pos)); game["positions"][sender]=final; game["last_roll"]=roll
            img=self._render_snake_board(game); url=self._game_public_image(img) if img else ""
            for r in self._game_rooms(game):
                if url:self.send_room_media(r,url,"image")
                self.send_room_text(r,f"🎲 @{sender} وقف الرول على {roll} وانتقل من {old_pos} إلى {final}.")
            if final>=100:
                photo=self.user_photos.get(str(sender).casefold(), "") or self._lookup_profile_photo(sender); win_img=self._render_snake_board(game,winner_name=sender)
                award = _add_points(sender, self.SNAKE_WIN_REWARD, repay_loan=True, return_details=True)
                self._notify_loan_repayment(sender, award)
                new_points = int(award.get("balance", 0) or 0)
                self._broadcast_game_result_all_rooms(f"🏆 فاز @{sender} بلعبة السلم والثعبان!\n🎲 الرول الأخير: {roll}\n📍 وصل إلى الخانة 100.\n💰 جائزة الفوز: +{self.SNAKE_WIN_REWARD:,} نقطة\n💸 سداد السلفة: {_fmt_points(award.get('loan_repayment', 0))} نقطة\n💳 رصيده الآن: {new_points:,} نقطة",win_img,"");
                try: game.get("timeout_timer").cancel()
                except Exception: pass
                self.snake_games.pop(key,None)
            else:
                game["turn"]=(game.get("turn",0)+1)%len(game["players"])
                next_player = game["players"][game["turn"]]
                for r in self._game_rooms(game):
                    self.send_room_text(r, f"🎯 الآن دور @{next_player}، اكتب rool.")
            return True
        return False

    def _ludo_track(self):
        """52 square cells around a square loop; no diagonal/arrow movement."""
        coords=[]
        # 14x14 perimeter = exactly 52 square cells.
        for c in range(14): coords.append((c,0))
        for r in range(1,14): coords.append((13,r))
        for c in range(12,-1,-1): coords.append((c,13))
        for r in range(12,0,-1): coords.append((0,r))
        return coords

    def _ludo_path_coords(self):
        """Authentic-looking 15x15 Ludo main track (52 cells, no numbers drawn)."""
        return [
            (6,0),(6,1),(6,2),(6,3),(6,4),(5,4),(4,4),(3,4),(2,4),(1,4),(0,4),
            (0,5),(0,6),(1,6),(2,6),(3,6),(4,6),(4,7),(4,8),(3,8),(2,8),(1,8),(0,8),
            (0,9),(0,10),(1,10),(2,10),(3,10),(4,10),(5,10),(6,10),(6,11),(6,12),(6,13),(6,14),
            (7,14),(8,14),(8,13),(8,12),(8,11),(8,10),(9,10),(10,10),(11,10),(12,10),(13,10),(14,10),
            (14,9),(14,8),(13,8),(12,8),(11,8),(10,8),(10,7),(10,6),(11,6),(12,6),(13,6),(14,6),
            (14,5),(14,4),(13,4),(12,4),(11,4),(10,4),(10,3),(10,2),(10,1),(10,0),(9,0),(8,0),
            (8,1),(8,2),(8,3),(8,4),(7,4)
        ][:52]

    def _ludo_track(self):
        # Main shared Ludo track: 52 cells. Each player has a different entry
        # point on this same track, then continues through a 6-cell home lane.
        return self._ludo_path_coords()

    def _ludo_player_color_index(self, game, username):
        try:
            return list(game.get("players", [])).index(username) % 4
        except Exception:
            return 0

    def _ludo_piece_coord(self, game, username, progress):
        """Return board coordinate for a player's progress.

        progress 1..52 = player's own 52-cell route, progress 53..58 =
        the six colored home-lane cells leading to the center.
        """
        track=self._ludo_track()
        color=self._ludo_player_color_index(game, username)
        progress=max(0,int(progress or 0))
        if progress <= 0:
            # Keep an unstarted token in its colored yard.
            yards=[(2,2),(11,2),(11,11),(2,11)]
            return yards[color]
        if progress <= 52:
            start_offsets=[0,13,26,39]
            return track[(start_offsets[color] + progress - 1) % 52]
        # Six final colored cells toward the center.
        home_lanes=[
            [(6,5),(6,6),(6,7),(7,7),(7,6),(7,7)],
            [(9,6),(8,6),(7,6),(7,7),(7,7),(7,7)],
            [(8,9),(8,8),(8,7),(7,7),(7,7),(7,7)],
            [(5,8),(6,8),(7,8),(7,7),(7,7),(7,7)],
        ]
        lane=home_lanes[color]
        return lane[min(progress-53,len(lane)-1)]

    def _render_ludo_board(self,state,winner_name=""):
        if not PIL_AVAILABLE:return None
        from PIL import Image,ImageDraw
        W=1050; H=1050; cell=58; ox=60; oy=60
        img=Image.new("RGB",(W,H),(20,24,31)); d=ImageDraw.Draw(img)
        colors=[(225,55,65),(75,145,235),(72,185,95),(245,185,45)]
        light=[(255,225,225),(220,238,255),(222,248,226),(255,244,205)]

        # Wooden-style frame and clean 15x15 Ludo board.
        d.rounded_rectangle((ox-16,oy-16,ox+15*cell+16,oy+15*cell+16),radius=28,fill=(92,58,32),outline=(235,195,110),width=5)
        d.rounded_rectangle((ox,oy,ox+15*cell,oy+15*cell),radius=10,fill=(245,245,245),outline=(35,45,55),width=3)

        # Four colored home yards: 6x6 corners with four player slots each.
        homes=[(0,0,colors[0]),(9,0,colors[1]),(0,9,colors[2]),(9,9,colors[3])]
        for hx,hy,col in homes:
            d.rectangle((ox+hx*cell,oy+hy*cell,ox+(hx+6)*cell,oy+(hy+6)*cell),fill=col,outline=(255,255,255),width=3)
            d.rounded_rectangle((ox+(hx+1)*cell,oy+(hy+1)*cell,ox+(hx+5)*cell,oy+(hy+5)*cell),radius=20,fill=(250,250,250),outline=(255,255,255),width=3)
            for px,py in ((2,2),(4,2),(2,4),(4,4)):
                cx=ox+(hx+px)*cell; cy=oy+(hy+py)*cell
                d.ellipse((cx-17,cy-17,cx+17,cy+17),fill=col,outline=(255,255,255),width=3)

        # Main 3-cell-wide cross lanes. No cell numbers: the board should look like real Ludo.
        for r in range(6,9):
            for c in range(15):
                # Leave colored home blocks intact.
                if c < 6 or c > 8:
                    d.rectangle((ox+c*cell,oy+r*cell,ox+(c+1)*cell,oy+(r+1)*cell),fill=(250,250,250),outline=(100,105,110),width=2)
        for c in range(6,9):
            for r in range(15):
                if r < 6 or r > 8:
                    d.rectangle((ox+c*cell,oy+r*cell,ox+(c+1)*cell,oy+(r+1)*cell),fill=(250,250,250),outline=(100,105,110),width=2)

        # Player-colored entry lanes toward the center.
        lane_defs=[
            ((6,1),(6,5),colors[0]), ((9,6),(13,6),colors[1]),
            ((8,9),(8,13),colors[3]), ((1,8),(5,8),colors[2])
        ]
        for (x1,y1),(x2,y2),col in lane_defs:
            if x1==x2:
                for y in range(y1,y2+1): d.rectangle((ox+x1*cell,oy+y*cell,ox+(x1+1)*cell,oy+(y+1)*cell),fill=light[colors.index(col)],outline=col,width=2)
            else:
                for x in range(x1,x2+1): d.rectangle((ox+x*cell,oy+y1*cell,ox+(x+1)*cell,oy+(y1+1)*cell),fill=light[colors.index(col)],outline=col,width=2)

        # Start cells with subtle arrows/markers.
        starts=[(6,0,colors[0]),(14,6,colors[1]),(8,14,colors[3]),(0,8,colors[2])]
        for x,y,col in starts:
            d.rectangle((ox+x*cell,oy+y*cell,ox+(x+1)*cell,oy+(y+1)*cell),fill=col,outline=(255,255,255),width=3)
            d.ellipse((ox+x*cell+17,oy+y*cell+17,ox+(x+1)*cell-17,oy+(y+1)*cell-17),outline=(255,255,255),width=3)

        # Center home triangle: the four colors meet in the classic Ludo finish.
        x0=ox+6*cell; y0=oy+6*cell; x1=ox+9*cell; y1=oy+9*cell; cx=(x0+x1)//2; cy=(y0+y1)//2
        d.polygon([(x0,y0),(x1,y0),(cx,cy)],fill=colors[0],outline=(255,255,255))
        d.polygon([(x1,y0),(x1,y1),(cx,cy)],fill=colors[1],outline=(255,255,255))
        d.polygon([(x1,y1),(x0,y1),(cx,cy)],fill=colors[3],outline=(255,255,255))
        d.polygon([(x0,y1),(x0,y0),(cx,cy)],fill=colors[2],outline=(255,255,255))

        # Safe/star markers on selected path cells.
        for x,y in [(2,6),(8,2),(12,8),(6,12),(8,6),(6,8)]:
            cx=ox+x*cell+cell/2; cy=oy+y*cell+cell/2
            d.text((cx,cy),"★",fill=(105,105,105),font=_gift_font("1",22),anchor="mm")

        # Player pieces follow each player's own route around the 52-cell
        # track, then enter that player's six-cell colored finish lane.
        for i,(u,pos) in enumerate(state.get("tokens",{}).items()):
            x,y=self._ludo_piece_coord(state,u,pos)
            avatar=self._game_square_avatar(u,44)
            cx=ox+x*cell+cell//2; cy=oy+y*cell+cell//2
            if avatar is not None:
                img.paste(avatar,(int(cx-22),int(cy-22)))
                d.ellipse((cx-24,cy-24,cx+24,cy+24),outline=(255,255,255),width=3)
            else:
                d.ellipse((cx-20,cy-20,cx+20,cy+20),fill=colors[i%4],outline=(255,255,255),width=3)
                d.text((cx,cy),str(i+1),fill=(255,255,255),font=_gift_font("1",18),anchor="mm")

        # Title and final winner badge. The winner photo is embedded in the final image.
        title="Ludo" if state.get("lang") == "en" else "لودو"
        d.rounded_rectangle((ox,18,ox+15*cell,oy-5),radius=16,fill=(16,30,45),outline=(230,190,75),width=3)
        d.text((ox+15*cell//2,42),title,fill=(245,205,80),font=_gift_font("1",30),anchor="mm")
        if winner_name:
            panel_y0 = oy + 15*cell + 8
            panel_y1 = min(H-8, panel_y0 + 100)
            d.rounded_rectangle((ox, panel_y0, ox+15*cell, panel_y1),
                                radius=18, fill=(16,30,45), outline=(245,198,70), width=3)
            winner_photo = ""
            try:
                winner_photo = self.user_photos.get(str(winner_name).casefold(), "") or self._lookup_profile_photo(winner_name)
            except Exception:
                pass
            avatar = _load_sender_avatar(winner_photo, 72) if winner_photo else None
            if avatar is not None:
                _paste_avatar_safe(img, avatar, (ox+20, panel_y0+14))
            d.text((ox+15*cell//2, panel_y0+23), "🏆 Winner" if state.get("lang") == "en" else "🏆 الفائز",
                   fill=(245,205,80), font=_gift_font("1",18), anchor="ma")
            _draw_name_centered(
                d, (ox+15*cell//2, panel_y0+80), str(winner_name).lstrip('@'), 24,
                (255,255,255), 15*cell - 180, apply_correction=True,
            )
        out=BASE_DIR/"generated_games"/f"ludo_{uuid.uuid4().hex}.jpg"; out.parent.mkdir(parents=True,exist_ok=True)
        img.save(out,"JPEG",quality=88,optimize=True); return out

    def _ludo_command(self,room,sender,raw):
        with self._ludo_lock:
            return self._ludo_command_unlocked(room, sender, raw)

    def _ludo_command_unlocked(self,room,sender,raw):
        key=f"ludo:{_norm_room(room)}"; low=str(raw or "").strip().casefold()
        low=low.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
        game=self.ludo_games.get(key)
        if low in ("لودو","ludo") and not game:
            if not self._board_game_cooldown_notice(room, sender):
                return True
            room_language = self._effective_room_language(room)
            game={"players":[sender],"tokens":{sender:0},"lang":room_language or ("en" if low=="ludo" else "ar"),"turn":0,"created":time.time(),"rooms":{room},"origin_room":room,"max_players":0,"bot":False,"started":False,"last_roll_at":0.0}
            self.ludo_games[key]=game; self._schedule_board_game_timeout(key, game, "لودو")
            self.send_room_text(room,"🎲 Ludo — choose 1–4 players.\n1 = You vs. the bot; 2–4 = multiplayer.\nOther players type join. When the game starts, type rool on your turn to roll the die and move. First token to the finish wins." if game["lang"] == "en" else "🎲 لودو: اختر عدد اللاعبين\n1 مع البوت\n2 لاعبين\n3 لاعبين\n4 لاعبين\nاكتب join للانضمام، ثم rool عند دورك لرمي النرد وتحريك القطعة. أول لاعب يصل للنهاية يفوز."); return True
        if not game:return False
        origin_room = str(game.get("origin_room") or next(iter(game.get("rooms", {room})), room))
        game["origin_room"] = origin_room
        # JOIN may be typed in any room, but an active Ludo game belongs to
        # its origin room; do not let another room silently join it.
        if room != origin_room:
            if low in ("join", "انضمام"):
                self.send_room_text(room, f"🎮 لديك لعبة لودو شغالة بالفعل في غرفة: {origin_room}\n✏️ اكتب اسم اللعبة لبدء لعبة جديدة.")
            return True
        game.setdefault("rooms",set()).add(origin_room)
        if low in ("لودو","ludo"):
            self.send_room_text(room,f"🎲 توجد لعبة لودو شغالة بالفعل في غرفة: {origin_room}. اختر العدد أو اكتب join/انضمام."); return True
        if low in ("1","2","3","4") and len(game["players"])==1 and not game.get("started"):
            count=int(low); game["max_players"]=1 if count==1 else count; game["bot"]=(count==1)
            if count==1:
                if "🤖 البوت" not in game["players"]:
                    game["players"].append("🤖 البوت")
                    game["tokens"]["🤖 البوت"]=0
                game["started"]=True
                game["turn"]=0
                self._send_game_cover("ludo",game)
                self._broadcast_game_start("🤖 بدأت لعبة لودو مع البوت! أنت تبدأ أولاً، اكتب rool للعب.",game)
            else:
                needed=count-1
                self.send_room_text(room,f"🎲 تم اختيار لودو لـ {count} لاعبين.\n👥 اكتب join للانضمام — نحتاج {needed} لاعباً إضافياً.\n📌 يبدأ اللعب بعد اكتمال العدد، ثم اكتب rool.")
            return True
        if low in ("join","انضمام") and not game.get("started"):
            max_players=int(game.get("max_players",4) or 4)
            if max_players <= 1:
                self.send_room_text(room,"⚠️ هذه اللعبة مع البوت. لا تحتاج join.")
                return True
            if sender not in game["players"] and len(game["players"]) < max_players:
                game["players"].append(sender)
                game["tokens"][sender]=0
                game["rooms"].add(room)
                self._schedule_board_game_timeout(key,game,"لودو")
                if len(game["players"]) >= max_players:
                    game["started"]=True
                    game["turn"]=0
                    self._send_game_cover("ludo",game)
                    names="، ".join(str(x).lstrip("@") for x in game["players"])
                    self.send_room_text(room,f"🎲 بدأت لعبة لودو!\n👥 اللاعبون: {names}\n🎯 دور {str(game['players'][0]).lstrip('@')}، اكتب rool.")
                else:
                    remaining=max_players-len(game["players"])
                    self.send_room_text(room,f"✅ انضم اللاعب.\n👥 باقي {remaining} لاعب/لاعبين ثم تبدأ اللعبة.")
            return True
        if self._game_roll_command(raw) and sender in game["players"]:
            if game.get("max_players",0)==0:self.send_room_text(room,"❌ اختر عدد اللاعبين أولاً: 1 أو 2 أو 3 أو 4."); return True
            if len(game["players"])<int(game["max_players"]):self.send_room_text(room,"⏳ ما زلنا ننتظر اكتمال عدد اللاعبين."); return True
            if game["players"].index(sender)!=game.get("turn",0):self.send_room_text(room,"⏳ انتظر دورك."); return True
            now=time.monotonic()
            if now-float(game.get("last_roll_at",0.0) or 0.0)<0.45:return True
            game["last_roll_at"]=now; game["started"]=True; self._schedule_board_game_timeout(key, game, "لودو")
            roll=secrets.randbelow(6)+1; old=game["tokens"].get(sender,0); new=min(58,old+roll); game["tokens"][sender]=new
            img=self._render_ludo_board(game); url=self._game_public_image(img) if img else ""
            for r in self._game_rooms(game):
                if url:self.send_room_media(r,url,"image")
                self.send_room_text(r,f"🎲 @{sender} وقف الرول على {roll} وانتقل من المربع {old} إلى {new}.")
            if new>=58:
                win_img=self._render_ludo_board(game,winner_name=sender); photo=self.user_photos.get(str(sender).casefold(), "") or self._lookup_profile_photo(sender)
                award = _add_points(sender, self.LUDO_WIN_REWARD, repay_loan=True, return_details=True)
                self._notify_loan_repayment(sender, award)
                new_points = int(award.get("balance", 0) or 0)
                self._broadcast_game_result_all_rooms(f"🏆 مبروك! فاز @{sender} بلعبة لودو.\n🎲 الرول الأخير: {roll}\n📍 وصل إلى نهاية المسار.\n💰 جائزة الفوز: +{self.LUDO_WIN_REWARD:,} نقطة\n💸 سداد السلفة: {_fmt_points(award.get('loan_repayment', 0))} نقطة\n💳 رصيده الآن: {new_points:,} نقطة",win_img,"");
                try: game.get("timeout_timer").cancel()
                except Exception: pass
                self.ludo_games.pop(key,None); return True
            game["turn"]=(game.get("turn",0)+1)%len(game["players"])
            if game.get("bot") and game["players"][game["turn"]]=="🤖 البوت":
                br=secrets.randbelow(6)+1
                bot_old=game["tokens"].get("🤖 البوت",0)
                bot_new=min(58,bot_old+br)
                game["tokens"]["🤖 البوت"]=bot_new
                # Bot move is followed by the human turn.
                game["turn"]=0
                game["last_roll_at"]=time.monotonic()
                bot_img=self._render_ludo_board(game); bot_url=self._game_public_image(bot_img) if bot_img else ""
                for r in self._game_rooms(game):
                    if bot_url:self.send_room_media(r,bot_url,"image")
                    human_player = game["players"][0] if game.get("players") else sender
                    self.send_room_text(r, f"🤖 البوت رمى {br} وانتقل من المربع {bot_old} إلى {bot_new}.\n🎯 الآن دور @{human_player}، اكتب rool.")
            else:
                next_player = game["players"][game["turn"]]
                for r in self._game_rooms(game):
                    self.send_room_text(r, f"🎯 الآن دور @{next_player}، اكتب rool.")
            return True
        return False

    def _marriage_game(self, room, sender):
        candidates=self._fun_room_members(room, sender)
        if not candidates:
            if self._effective_room_language(room) == "en":
                self.send_room_text(room, f"💍 @{sender}, no other player is available for the wedding game right now.")
            else:
                self.send_room_text(room, f"💍 @{sender} لم أجد عضواً آخر متاحاً للزواج حالياً.")
            return True
        partner=secrets.choice(candidates)
        if self._effective_room_language(room) == "en":
            self.send_room_text(room, f"💍 Congratulations, @{sender}! Your spouse is @{partner} ❤️\nWishing you both a happy life full of joy.")
            return True
        sentence=(f"زوجتك هي @{partner}" if secrets.randbelow(2) == 0
                  else f"زوجك هو @{partner}")
        self.send_room_text(room, f"💍 مبروك @{sender}! {sentence} ❤️\n"
                                 "نتمنى لكما حياة سعيدة مليئة بالفرح.")
        return True

    def _social_pair_game(self, room, sender, command):
        candidates = self._fun_room_members(room, sender)
        english = self._effective_room_language(room) == "en"
        if not candidates:
            self.send_room_text(room, f"🤝 @{sender}, no other player is available in this room." if english else f"🤝 @{sender} لم أجد عضواً آخر حاضراً في هذه الغرفة.")
            return True
        partner = secrets.choice(candidates)
        score = secrets.randbelow(101)
        kind = str(command or "").replace("ة", "ه")
        if english:
            labels = {"خطبه": "your fiancé", "خطيب": "your fiancé", "حبك": "your love match", "عدو": "your playful rival", "صديق": "your closest friend", "زاحف": "the biggest chatterbox", "خروف": "the room's playful scapegoat"}
            label = labels.get(kind, "your match")
            if kind in {"خطبه", "خطيب"}:
                result = f"💍 Lovely news, @{sender}: your fiancé is @{partner}. Wishing you both happiness!"
            elif kind == "حبك":
                result = f"❤️ @{sender}, {label} is @{partner} — compatibility: {score}%. Looks like a great match!"
            elif kind == "عدو":
                result = f"😼 @{sender}, {label} today is @{partner} ({score}%). Make peace with a laugh!"
            elif kind == "صديق":
                result = f"🫶 @{sender}, your closest friend today is @{partner} — friendship score: {score}%!"
            elif kind == "زاحف":
                result = f"🦎 @{sender}, today's biggest chatterbox is @{partner} — score: {score}% (just for fun)."
            else:
                result = f"🐑 @{sender}, the room's playful scapegoat is @{partner} ({score}%). It's all a joke!"
            self.send_room_text(room, result)
            return True
        if kind in {"خطبه", "خطيب"}:
            result = f"💍 خبرٌ جميل يا @{sender}: خطيبك هو @{partner}.\nنتمنى لكما أياماً مليئة بالمودة والفرح."
        elif kind == "حبك":
            result = f"❤️ حبك هو @{partner} بنسبة {score}% — يبدو أن بينكما انسجاماً جميلاً!"
        elif kind == "عدو":
            result = f"😼 عدوك في المزاح اليوم هو @{partner} بنسبة {score}% — صلّحوا الأمور بالضحك!"
        elif kind == "صديق":
            result = f"🫶 صديقك الأقرب اليوم هو @{partner} بنسبة صداقة {score}%!"
        elif kind == "زاحف":
            result = f"🦎 أكثر شخص يلاحق السوالف اليوم: @{partner} — نسبة الزحف {score}% (مزاحاً)."
        else:
            result = f"🐑 خروف الغرفة بالمزاح هو @{partner} بنسبة {score}% — لا تزعلوا، كلها لعبة!"
        self.send_room_text(room, result)
        return True

    def _random_social_hunt_game(self, room, sender, command):
        """Pick a live room member and send a profile/fallback portrait."""
        english = self._effective_room_language(room) == "en"
        female_photo = str(command or "").strip() == "صيد مزه"
        requested_gender = "female" if female_photo else "male"
        room_members = self._fun_room_members(room, sender)
        # Both hunt variants must select from the live roster of this room and
        # must not fall back to a mixed/unknown candidate.
        candidates = [
            member for member in room_members
            if self._picture_gender_hint(member) == requested_gender
        ]
        # If no member matches the requested gender, keep the game usable by
        # choosing randomly from the same room roster as a fallback.
        if not candidates:
            candidates = list(room_members)
        if not candidates:
            self.send_room_text(
                room,
                ("No other male room member is available for this game." if requested_gender == "male"
                 else "No other female room member is available for this game.") if english else
                ("لم أجد عضواً ذكراً آخر متاحاً في الغرفة لهذه اللعبة." if requested_gender == "male"
                 else "لم أجد عضوةً أنثى أخرى متاحةً في الغرفة لهذه اللعبة."),
            )
            return True
        if not self._game_cooldown_notice(room, sender, 30.0, str(command)):
            return True
        target = secrets.choice(candidates)
        target_key = _norm_user(target)
        photo_url = str(getattr(self, "user_photos", {}).get(target_key, "") or "").strip()
        if not photo_url:
            photo_url = self._lookup_profile_photo(target)
        fallback_used = False
        if not photo_url:
            # A missing Talkin profile photo must not stop the game. Search a
            # gender-specific, SFW portrait on the public image sources; the
            # final LoremFlickr URL keeps the game usable if search providers
            # temporarily block the hosting IP.
            query = "beautiful woman portrait safe for work" if female_photo else "handsome man portrait safe for work"
            try:
                photo_url = _search_lookalike_image(query)
            except Exception as exc:
                self.log("[SOCIAL-HUNT] fallback image search failed:", repr(exc))
            if not photo_url:
                lock = secrets.randbelow(1_000_000) + 1
                tags = "woman,portrait" if female_photo else "man,portrait"
                photo_url = f"https://loremflickr.com/640/480/{tags}?lock={lock}"
            fallback_used = bool(photo_url)
        image_sent = False
        if photo_url:
            try:
                image_sent = self.send_room_media(room, photo_url, "image") is not False
            except Exception as exc:
                self.log("[SOCIAL-HUNT] image delivery failed:", room, repr(exc))
        if not image_sent and not fallback_used:
            # A saved profile URL may be stale or inaccessible; retry with a
            # gender-matched public portrait before reporting a missing image.
            query = "beautiful woman portrait safe for work" if female_photo else "handsome man portrait safe for work"
            try:
                fallback_url = _search_lookalike_image(query, exclude_urls=[photo_url] if photo_url else None)
            except Exception as exc:
                self.log("[SOCIAL-HUNT] fallback image search failed:", repr(exc))
                fallback_url = None
            if not fallback_url:
                lock = secrets.randbelow(1_000_000) + 1
                tags = "woman,portrait" if female_photo else "man,portrait"
                fallback_url = f"https://loremflickr.com/640/480/{tags}?lock={lock}"
            try:
                image_sent = self.send_room_media(room, fallback_url, "image") is not False
                fallback_used = image_sent
            except Exception as exc:
                self.log("[SOCIAL-HUNT] fallback image delivery failed:", room, repr(exc))
        hunter_plain, target_plain = str(sender).strip().lstrip("@"), str(target).strip().lstrip("@")
        if english:
            label = "Beauty Hunt" if female_photo else "Handsome Hunt"
            action = f"@{hunter_plain} caught a beauty: @{target_plain}" if female_photo else f"@{hunter_plain} caught a handsome man: @{target_plain}"
            result = f"🎯 {label}!\n👤 {action}.\n🖼️ This is the selected member's profile photo."
            if not image_sent:
                result += "\n⚠️ The portrait could not be downloaded."
            elif fallback_used:
                result += "\n🖼️ A random gender-matched internet portrait was used because no profile photo was available."
        else:
            label_ar = "صيد المزة" if female_photo else "صيد المز"
            action = f"@{hunter_plain} اصطاد مزه: @{target_plain}" if female_photo else f"@{hunter_plain} اصطادت مز: @{target_plain}"
            result = (
                f"🎯 {label_ar}!\n👤 {action}.\n"
                "🖼️ هذه صورة بروفايل العضو المختار."
            )
            if not image_sent:
                result += "\n⚠️ تعذر تحميل الصورة البديلة حالياً."
            elif fallback_used:
                result += "\n🖼️ لم توجد صورة بروفايل؛ تم استخدام صورة عشوائية من الإنترنت مناسبة للنوع."
        self.send_room_text(room, result)
        return True

    def _court_social_game(self, room, sender, command):
        """Lighthearted room-only ex/khul/divorce game; not a real legal result."""
        english = self._effective_room_language(room) == "en"
        candidates = self._fun_room_members(room, sender)
        if not candidates:
            self.send_room_text(
                room,
                "No other room member is available for this game." if english
                else "لم أجد عضواً آخر متاحاً في الغرفة لهذه اللعبة.",
            )
            return True
        partner = secrets.choice(candidates)
        kind = str(command or "").strip()
        if kind == "طليقي":
            text = (
                f"💔 @{sender}, your ex is @{partner}. (For fun only.)"
                if english else f"💔 @{sender}، طليقك هو @{partner}. (لعبة مزاح فقط)"
            )
            self.send_room_text(room, text)
            return True
        self.send_room_text(
            room,
            "⌛ Awaiting the court's ruling..." if english else "⌛ بانتظار حكم المحكمة...",
        )
        if kind == "خلع":
            result = (
                f"⚖️ The game court grants khulʿ for @{sender} from @{partner}.\n"
                f"💰 All expenses are returned to @{sender}. (For fun only.)"
                if english else
                f"⚖️ حكم القاضي بخلع @{sender} من @{partner}.\n"
                f"💰 رُدّت كامل النفقات للمخلوع @{sender}. (لعبة مزاح فقط)"
            )
        else:
            result = (
                f"⚖️ The game court grants triple divorce to @{partner}.\n"
                f"✍️ Initiator: @{sender}. (For fun only.)"
                if english else
                f"⚖️ حكم القاضي بطلاق @{partner} طلاقاً بالثلاث.\n"
                f"✍️ المطلق: @{sender}. (لعبة مزاح فقط)"
            )
        self.send_room_text(room, result)
        return True

    def _celebrity_partner_game(self, room, sender, gender):
        """Find a real public celebrity portrait and send it to the room."""
        female = [
            ("نانسي عجرم", "Nancy Ajram"), ("إليسا", "Elissa Lebanese singer"),
            ("أصالة نصري", "Assala Nasri"), ("أحلام الشامسي", "Ahlam Al Shamsi"),
            ("نجوى كرم", "Najwa Karam"), ("شيرين عبد الوهاب", "Sherine Abdel-Wahab"),
            ("بيونسيه", "Beyonce"), ("تايلور سويفت", "Taylor Swift"),
        ]
        male = [
            ("ماجد المهندس", "Majid Al Mohandis"), ("كاظم الساهر", "Kadim Al Sahir"),
            ("عمرو دياب", "Amr Diab"), ("محمد عبده", "Mohammed Abdu"),
            ("تامر حسني", "Tamer Hosny"), ("عادل إمام", "Adel Emam"),
            ("توم كروز", "Tom Cruise"), ("ليوناردو دي كابريو", "Leonardo DiCaprio"),
        ]
        names = female if gender == "female" else male
        busy = getattr(self, "_celebrity_partner_busy", set())
        key = (_norm_user(sender), _norm_room(room))
        if key in busy:
            self.send_room_text(room, f"⏳ @{sender} جاري البحث عن الصورة...")
            return True
        busy.add(key)
        self._celebrity_partner_busy = busy
        self.send_room_text(room, "🔎 جاري البحث عن صورة شخصية مشهورة مناسبة...")
        response_language = self._capture_room_response_language(room)

        def worker():
            try:
                selected_name = ""
                selected_url = ""
                failures = []
                candidates = secrets.SystemRandom().sample(names, len(names))
                for display_name, english_name in candidates:
                    # Search the Arabic name and the well-known Latin spelling;
                    # Wikipedia/Wikimedia often index one but not the other.
                    for subject in (english_name, display_name):
                        try:
                            image_urls = _search_lookalike_images(subject, limit=12)
                        except Exception as exc:
                            failures.append(f"search:{type(exc).__name__}")
                            continue
                        if not image_urls:
                            failures.append(f"empty-search:{subject}")
                            continue
                        for image_url in image_urls:
                            check = None
                            try:
                                # Some legitimate image hosts ignore Range or use
                                # application/octet-stream; inspect bytes as well.
                                check = requests.get(
                                    image_url,
                                    headers={"User-Agent": "Mozilla/5.0 TalkinBot/1.0", "Range": "bytes=0-63"},
                                    timeout=(4, 8), stream=True,
                                )
                                status = int(getattr(check, "status_code", 0) or 0)
                                content_type = str(check.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                                sample = b""
                                try:
                                    sample = check.raw.read(64, decode_content=True)
                                except Exception:
                                    try:
                                        sample = next(check.iter_content(64), b"")
                                    except Exception:
                                        pass
                                valid_magic = (
                                    sample.startswith(b"\xff\xd8\xff")
                                    or sample.startswith(b"\x89PNG\r\n\x1a\n")
                                    or sample[:6] in (b"GIF87a", b"GIF89a")
                                    or (sample[:4] == b"RIFF" and sample[8:12] == b"WEBP")
                                )
                                if status not in (200, 206) or not (content_type.startswith("image/") or valid_magic):
                                    failures.append(f"not-image:{content_type or status}")
                                    continue
                            except Exception as exc:
                                failures.append(f"verify:{type(exc).__name__}")
                                continue
                            finally:
                                if check is not None:
                                    try:
                                        check.close()
                                    except Exception:
                                        pass
                            try:
                                result = self.send_room_media(room, image_url, "image")
                                if result is False:
                                    failures.append("talkin-rejected-image")
                                    continue
                                selected_name, selected_url = display_name, image_url
                                break
                            except Exception as exc:
                                failures.append(f"send:{type(exc).__name__}")
                                self.log("[CELEBRITY_PARTNER] candidate failed:", display_name, repr(exc))
                        if selected_url:
                            break
                    if selected_url:
                        break
                if not selected_url:
                    raise RuntimeError("no usable celebrity image found; " + ",".join(failures[-12:]))
                if self._effective_room_language(room) == "en":
                    label = "your wife" if gender == "female" else "your husband"
                    self.send_room_text(room, f"💞 @{sender}, {label} is the celebrity {selected_name} ❤️")
                else:
                    label = "زوجتك" if gender == "female" else "زوجك"
                    self.send_room_text(room, f"💞 @{sender}، {label} هي الشخصية المشهورة: {selected_name} ❤️")
                self.log("[CELEBRITY_PARTNER] direct image delivered:", sender, selected_name, selected_url)
            except Exception as exc:
                self.log("[CELEBRITY_PARTNER] failed:", repr(exc))
                try:
                    # Keep the familiar user-facing failure message; detailed
                    # diagnostics remain in the server log for the owner.
                    self.send_room_text(room, "❌ تعذر العثور على صورة مناسبة الآن؛ جرّب مرة أخرى لاحقاً.")
                except Exception:
                    pass
            finally:
                busy.discard(key)

        threading.Thread(target=self._localized_room_worker(room, response_language, worker), name="celebrity-partner-search", daemon=True).start()
        return True

    def _fun_game(self, room, sender, command):
        if self._effective_room_language(room) == "en":
            options = {
                "تحدي": ["Send a kind message to someone in the room.", "Write one thing you love today.", "Compliment someone you haven't talked to much.", "Tell a short joke for everyone."],
                "لغز": [("What has teeth but cannot bite?", "A comb."), ("What moves without legs?", "Time."), ("What gets bigger the more you take away?", "A hole.")],
                "حظي": ["Your luck is great today — try something new!", "Luck is smiling on you; go for it.", "A calm, lovely day is ahead.", "A nice surprise may arrive soon."],
                "مزاج": ["Your mood: joyful and full of positive energy 😄", "Your mood: calm and relaxed 🌿", "Your mood: adventurous and excited 🔥", "Your mood: laughing and chatting 😂"],
            }
            value = secrets.choice(options[command])
            label = {"تحدي": "Challenge", "لغز": "Riddle", "حظي": "My luck", "مزاج": "Mood"}[command]
            if command == "لغز":
                question, answer = value
                self.send_room_text(room, f"🧩 Riddle for @{sender}\n{question}\n✅ Answer: {answer}")
            elif command == "تحدي":
                self.send_room_text(room, f"🎯 Challenge for @{sender}: {value}")
            else:
                self.send_room_text(room, f"✨ {label} for @{sender}: {value}")
            return True
        options={
            "تحدي": ["أرسل كلمة طيبة لعضو في الغرفة.", "اكتب أول شيء تحبه اليوم.", "امدح شخصاً لم تتحدث معه كثيراً.", "اكتب نكتة قصيرة للجميع."],
            "لغز": [("شيء له أسنان ولا يعض، ما هو؟", "المشط"), ("ما الشيء الذي يمشي بلا أرجل؟", "الوقت"), ("ما الذي كلما أخذت منه كبر؟", "الحفرة")],
            "حظي": ["حظك اليوم ممتاز، جرّب شيئاً جديداً!", "الحظ يبتسم لك، لا تتردد.", "يوم هادئ وجميل بانتظارك.", "مفاجأة لطيفة قد تصلك قريباً."],
            "مزاج": ["مزاجك اليوم: فرح وطاقة إيجابية 😄", "مزاجك اليوم: هدوء وراحة 🌿", "مزاجك اليوم: مغامرة وحماس 🔥", "مزاجك اليوم: ضحك وسوالف 😂"],
        }
        value=secrets.choice(options[command])
        if command == "لغز":
            question, answer=value
            self.send_room_text(room, f"🧩 لغز @{sender}\n{question}\n✅ الإجابة: {answer}")
        elif command == "تحدي":
            self.send_room_text(room, f"🎯 تحدي @{sender}: {value}")
        else:
            self.send_room_text(room, f"✨ {command} @{sender}: {value}")
        return True

    def _send_treasure_game_result_card(self, room, game_key, winner_name):
        """Send a personalized game result card only to rooms containing participants."""
        if _game_results_disabled_for_room(room):
            return False
        base = _public_base_url()
        if not base:
            self.log("[TREASURE-CYCLE] result image skipped: public media URL is not configured")
            return False
        config = TIMED_TREASURE_GAMES.get(game_key, TIMED_TREASURE_GAMES["rare_diamond"])
        source = ASSETS_DIR / "treasures" / config["image"]
        if not source.is_file():
            stem = Path(config["image"]).stem
            candidates = [ASSETS_DIR / "treasures" / f"{stem}.{ext}" for ext in ("png", "jpg", "jpeg")]
            candidates.extend([ASSETS_DIR / "treasures" / "rare_diamond_3d.png", ASSETS_DIR / "treasures" / "rare_diamond_3d.jpg"] if game_key == "rare_diamond" else [])
            source = next((candidate for candidate in candidates if candidate.is_file()), None)
        if not source or not Path(source).is_file():
            self.log("[TREASURE-CYCLE] missing result art:", config["image"])
            return False
        try:
            winner_key = _norm_user(winner_name)
            winner_photo = getattr(self, "user_photos", {}).get(winner_key, "")
            if not winner_photo:
                winner_photo = self._lookup_profile_photo(winner_name)
            language_en = self._effective_room_language(room) == "en"
            title = config["winner_en"] if language_en else config["winner_ar"]
            card = render_rare_diamond_winner_card(winner_name, winner_photo, source_path=source, title=title)
            url = f"{base}/games/{card.name}"
            self._verify_public_media_url(url, "image")
            sent = self.send_room_media(room, url, "image") is not False
            self.log("[TREASURE-CYCLE] result card", game_key, room, "sent=" + str(sent))
            return sent
        except Exception as exc:
            self.log("[TREASURE-CYCLE] result card failed:", game_key, repr(exc))
            return False

    def handle_game_command(self, room, text, sender_name):
        original_raw = str(text or "").strip()
        auto_gold = original_raw.casefold() == "gold auto"
        pending_gold = getattr(self, "_stock_auto_gold_pending", {})
        pending_gold_key = (_norm_room(room), _norm_user(sender_name))
        confirm_gold = (
            original_raw.casefold() in {"تأكيد", "موافق", "أوافق", "قبول", "تأكيد ذهب", "تأكيد الذهب", "confirm", "confirm gold"}
            and isinstance(pending_gold, dict) and pending_gold_key in pending_gold
        )
        stock_order = _parse_stock_market_order(text)
        wallet_order = _parse_wallet_purchase(text)
        raw=_normalize_game_command_text(text)
        raw_digits = raw.translate(str.maketrans(
            "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"
        ))
        if stock_order:
            raw = "بورصة"
        if not raw or not sender_name: return False
        if auto_gold:
            if not _is_verified_user(sender_name):
                self.send_room_text(room, f"🔒 @{sender_name} حسابك غير موثق لاستخدام الألعاب.\n{_verification_notice()}")
                return True
            if not _games_enabled_for_room(room):
                self.send_room_text(room, "🛑 الألعاب متوقفة في هذه الغرفة حالياً.")
                return True
            amount = min(max(0, _get_points(sender_name)), STOCK_MARKET_MAX_INVESTMENT)
            if amount < 10:
                self.send_room_text(room, "❌ رصيدك أقل من الحد الأدنى 10 نقاط.")
                return True
            pending = getattr(self, "_stock_auto_gold_pending", {})
            pending[(_norm_room(room), _norm_user(sender_name))] = {"amount": amount, "created": time.time()}
            self._stock_auto_gold_pending = pending
            if self._configured_room_language(room) == "en":
                notice = f"⚠️ Investment risk: gold can fall and you could lose the full amount. Your current balance ({_fmt_points(amount)} points) will be invested automatically, capped at 30 billion (30b). To accept, type: confirm (within 1 minute)."
            else:
                notice = f"⚠️ تحذير استثماري: سعر الذهب قد يهبط وقد تخسر كامل المبلغ. سيُستثمر رصيدك الحالي تلقائياً ({_fmt_points(amount)} نقطة)، وبحد أقصى 30 مليار (30b). إذا كنت موافقاً أرسل: موافق (خلال دقيقة)."
            self.send_room_text(room, notice)
            return True
        if confirm_gold:
            if not _is_verified_user(sender_name):
                self.send_room_text(room, f"🔒 @{sender_name} حسابك غير موثق لاستخدام الألعاب.\n{_verification_notice()}")
                return True
            if not _games_enabled_for_room(room):
                self.send_room_text(room, "🛑 الألعاب متوقفة في هذه الغرفة حالياً.")
                return True
            pending = getattr(self, "_stock_auto_gold_pending", {})
            order = pending.pop((_norm_room(room), _norm_user(sender_name)), None)
            if not order or time.time() - float(order.get("created", 0)) > 60:
                self.send_room_text(room, "⌛ انتهت مهلة التأكيد؛ أرسل بورصة لعرض التحذير من جديد.")
                return True
            amount = min(int(order.get("amount", 0)), max(0, _get_points(sender_name)), STOCK_MARKET_MAX_INVESTMENT)
            if amount < 10:
                self.send_room_text(room, "❌ رصيدك الحالي لا يكفي للاستثمار.")
                return True
            if not self._stock_market_is_open():
                self._start_stock_market_round(first_room=room)
            return self._stock_market_invest(room, sender_name, 1, amount)
        bomb_key = _game_choice_key(room, sender_name)
        pending_bomb = getattr(self, "_pending_bomb_targets", {}).get(bomb_key)
        if pending_bomb:
            target = str(raw).strip().lstrip("@").split()[0] if raw else ""
            if target and not _looks_like_bot_command(raw):
                getattr(self, "_pending_bomb_targets", {}).pop(bomb_key, None)
                return self._bomb_command(room, sender_name, target, pending_bomb.get("target_room") or room)
        if raw.casefold() == "bom":
            if not _room_manager(self, room, sender_name):
                self.send_room_text(room, "🔒 بدء القنبلة مخصص للماستر أو إدارة الغرفة.")
                return True
            pending = getattr(self, "_pending_bomb_targets", None)
            if not isinstance(pending, dict):
                pending = {}
                self._pending_bomb_targets = pending
            pending[bomb_key] = {"target_room": room, "created": time.time()}
            self.send_room_text(room, "👤 أرسل اسم المستخدم المستهدف بالقنبلة الآن، وسيتم تنفيذ الأمر في هذه الغرفة.")
            return True
        # Active board-game state always gets first priority.  This prevents
        # numeric Ludo choices (1-4), join and rool from being swallowed by
        # unrelated pending game prompts such as the stock exchange.
        low=raw.casefold()
        if self._snake_command(room,sender_name,raw): return True
        if self._ludo_command(room,sender_name,raw): return True
        if low in ("بورصتي", "سجل بورصتي", "my stock history", "my stock market"):
            return self._stock_history_command(room, sender_name)
        stock_key=(_norm_room(room), _norm_user(sender_name))
        pending_stock=self.stock_pending.get(stock_key)
        if pending_stock:
            if time.time()-float(pending_stock.get("created",0) or 0) >= 120:
                self.stock_pending.pop(stock_key,None)
                self.send_room_text(room,"⌛ انتهت مهلة البورصة. اكتب بورصة من جديد.")
                return True
            if pending_stock.get("kind") == "market_amount" and re.fullmatch(r"[0-9][0-9,]*(?:\.[0-9]+)?[kmb]?", raw_digits, re.I):
                amount = _parse_compact_amount(raw_digits)
                if amount is None:
                    self.send_room_text(room, "❌ مبلغ غير صالح. استخدم رقمًا مثل 100000 أو 1m أو 1b.")
                    return True
                self.stock_pending.pop(stock_key, None)
                return self._stock_market_invest(
                    room, sender_name, pending_stock.get("asset"), amount,
                    expected_round_id=pending_stock.get("event_id"),
                )
            if pending_stock.get("kind") == "market_choice" and re.fullmatch(r"[1-4]",raw_digits):
                return self._stock_exchange_game(room,sender_name,int(raw_digits))
        if self._wallet_handle_next_page(room, sender_name, raw):
            return True
        if self._handle_pending_bot_choice(room, raw, sender_name):
            return True
        join_alias = " ".join(raw.strip().casefold().replace("ة", "ه").split())
        if join_alias in {"انضمام لعبه ثعبان", "انضمام ثعبان", "انضمام لعبه لودو", "انضمام لودو"}:
            if not _is_verified_user(sender_name):
                self.send_room_text(room, f"🔒 @{sender_name} حسابك غير موثق لاستخدام الألعاب.\n{_verification_notice()}")
                return True
            if not _games_enabled_for_room(room):
                self.send_room_text(room, "🛑 الألعاب متوقفة في هذه الغرفة حالياً.")
                return True
            if "ثعبان" in join_alias:
                if self._snake_command(room, sender_name, "join"):
                    return True
                self.send_room_text(room, "⏳ لا توجد لعبة ثعبان نشطة في هذه الغرفة الآن.")
            else:
                if self._ludo_command(room, sender_name, "join"):
                    return True
                self.send_room_text(room, "⏳ لا توجد لعبة لودو نشطة في هذه الغرفة الآن.")
            return True
        # A bare stake (for example `1` or `100b`) may join the only open
        # wager. Never guess when multiple wager types are waiting.
        bare_stake_match = re.fullmatch(r"[0-9][0-9,]*(?:\.[0-9]+)?[kmb]?", raw_digits.strip(), re.I)
        if bare_stake_match:
            open_wagers = getattr(self, "wager_waiting", {})
            open_wagers = [
                (str(key), value) for key, value in (open_wagers.items() if isinstance(open_wagers, dict) else [])
                if isinstance(value, dict) and not value.get("_refund_pending")
            ]
            if len(open_wagers) == 1:
                if not _is_verified_user(sender_name):
                    self.send_room_text(room, f"🔒 @{sender_name} حسابك غير موثق لاستخدام الألعاب.\n{_verification_notice()}")
                    return True
                if not _games_enabled_for_room(room):
                    self.send_room_text(room, "🛑 الألعاب متوقفة في هذه الغرفة حالياً.")
                    return True
                game_key, waiting_wager = open_wagers[0]
                game_name = str(waiting_wager.get("game") or game_key)
                amount = _parse_compact_amount(raw_digits.strip())
                if amount is not None:
                    return self._queue_wager(room, sender_name, game_name, amount)
            if len(open_wagers) > 1:
                self.send_room_text(
                    room,
                    "⚠️ توجد رهانات متعددة معلّقة. اكتب اسم اللعبة مع مبلغ لا يقل عن 100k، مثل: رهان 100k أو مضاربة 100k.",
                )
                return True
        # Do not run the verification gate for ordinary conversation.
        if not _looks_like_bot_command(raw):
            return False
        normalized_raw = raw_digits.replace("ة", "ه")
        # Game-name normalization: Arabic ه/ة variants are treated as the same
        # command (e.g. سناره/سنارة, حصانه/حصانة), while preserving raw text
        # for commands that contain user arguments.
        game_low = low.replace("ة", "ه")

        # Primary-master diagnostics render the real winner template with the
        # configured master name and post it in the room where the command ran.
        image_check = {
            "فحص صورة المليون": "million", "فحص صوره المليون": "million",
            "فحص_صورة_المليون": "million",
            "فحص صورة المليار": "billion", "فحص صوره المليار": "billion",
            "فحص_صورة_المليار": "billion",
            "فحص سهم": "royal_dragon", "فحص الماسه": "royal_diamond",
        }.get(game_low)
        if image_check:
            if not _is_primary_master(sender_name):
                self.send_room_text(room, "🔒 فحص صور الجوائز مخصص للماستر الموجود في متغير BOT_MASTER.")
                return True
            if not room:
                self.send_private_text(sender_name, "⚠️ أرسل أمر الفحص داخل الغرفة التي تريد عرض الصورة فيها.")
                return True
            base = _public_base_url()
            if not base:
                self.send_private_text(sender_name, "❌ لا يوجد رابط عام للوسائط؛ تحقق من PUBLIC_MEDIA_URL أو PUBLIC_BASE_URL أو Railway Domain.")
                return True
            try:
                if not PIL_AVAILABLE:
                    raise RuntimeError("Pillow غير مثبت")
                winner_name = str(BOT_MASTER or sender_name).strip().lstrip("@")
                if image_check in {"royal_dragon", "royal_diamond"}:
                    avatar_url = (getattr(self, "user_photos", {}).get(_norm_user(winner_name), "")
                                  or self._lookup_profile_photo(winner_name))
                    card = self._render_royal_winner_card(image_check, winner_name, avatar_url)
                    if not card:
                        raise RuntimeError("تعذر إنشاء بطاقة الفوز الملكية")
                    optimized = _optimize_social_media_asset(card)
                    if not optimized:
                        raise RuntimeError("تعذر تجهيز صورة البطاقة للإرسال")
                    url = f"{base}/publish/{quote(Path(optimized).name)}"
                    label = "السهم / التنين الملكي 🐉" if image_check == "royal_dragon" else "الماسة الملكية 💎"
                else:
                    card = render_money_game_winner_card(image_check, winner_name)
                    url = f"{base}/games/{card.name}"
                    label = "المليون" if image_check == "million" else "المليار"
                self._verify_public_media_url(url, "image")
                self.send_room_text(room, f"🧪 اختبار بطاقة الفوز: {label} — @{winner_name}")
                if self.send_room_media(room, url, "image") is False:
                    raise RuntimeError("تعذر إرسال الصورة إلى الغرفة")
                self.send_private_text(sender_name, f"✅ أُرسلت بطاقة فحص {label} إلى الغرفة {room} باسم @{winner_name}.")
            except Exception as exc:
                self.send_private_text(sender_name, f"❌ تعذر إنشاء/إرسال صورة الفحص: {exc}")
            return True

        # Master-only loan and auction settings are handled before the normal
        # game gate so an admin command is not blocked by room game settings.
        master_repay = re.fullmatch(r"سدد\s*@?(.+)", raw.strip(), re.I | re.S)
        if master_repay and _norm_user(master_repay.group(1)) not in {"سلفتي", "سلفه", "السلفه", "السلفتي"}:
            if not _is_master_name(sender_name):
                self.send_private_text(sender_name, "🔒 أمر سداد سلفة مستخدم مخصص للماستر.")
                return True
            target = master_repay.group(1).strip().lstrip("@").strip()
            result = _repay_points_loan(target)
            if result.get("ok"):
                self.send_private_text(sender_name, f"✅ تم سداد سلفة @{target}: {_fmt_points(result['paid'])} نقطة. الرصيد المتبقي: {_fmt_points(result['balance'])}.")
            elif result.get("error") == "insufficient":
                self.send_private_text(sender_name, f"❌ لا يمكن سداد سلفة @{target}. الرصيد {_fmt_points(result.get('balance', 0))} أقل من السلفة المتبقية {_fmt_points(result.get('loan_debt', 0))}.")
            elif result.get("error") == "no_loan":
                self.send_private_text(sender_name, f"ℹ️ لا توجد سلفة قائمة على @{target}.")
            else:
                self.send_private_text(sender_name, f"❌ تعذر سداد سلفة @{target}.")
            return True

        rate_match = re.fullmatch(r"نسب(?:ه|ة)\s+مزاد\s+([0-9]+(?:[.,][0-9]+)?)", raw.strip(), re.I)
        if rate_match:
            if not _is_master_name(sender_name):
                self.send_private_text(sender_name, "🔒 تغيير نسبة المزاد مخصص للماستر.")
                return True
            try:
                rate = float(rate_match.group(1).replace(",", "."))
                from auction_game import set_redemption_rate_percent
                applied = set_redemption_rate_percent(rate)
                self.send_private_text(sender_name, f"✅ تم ضبط نسبة المزاد إلى {applied:g}% للربح والخسارة.")
            except Exception as exc:
                self.log("[AUCTION] rate update failed:", repr(exc))
                self.send_private_text(sender_name, "❌ النسبة غير صالحة. استخدم مثالاً: نسبة مزاد 4")
            return True

        # All games are available to verified accounts (including VIP).
        # The master remains allowed automatically by _is_verified_user().
        # This check is intentionally inside the game handler so game commands
        # can never become master-only because of an external command gate.
        if not _is_verified_user(sender_name):
            self.send_room_text(room, f"🔒 @{sender_name} حسابك غير موثق لاستخدام الألعاب.\n{_verification_notice()}")
            return True
        if low in ("العاب","ألعاب","لعب","games","game"):
            self.game_help(room, sender_name); return True
        if not _games_enabled_for_room(room):
            self.send_room_text(room, "🛑 الألعاب متوقفة في هذه الغرفة حالياً.")
            return True
        if self._royal_hunt_command(room, sender_name, original_raw):
            return True
        if game_low in {"سدد سلفتي", "سدد سلفه", "سدد السلفه", "سدد السلفتي", "repay loan", "repay my loan"}:
            result = _repay_points_loan(sender_name)
            if result.get("ok"):
                self.send_room_text(room, f"✅ تم سداد سلفتك بالكامل: {_fmt_points(result['paid'])} نقطة. رصيدك الآن: {_fmt_points(result['balance'])}.")
            elif result.get("error") == "insufficient":
                self.send_room_text(room, f"❌ لا يمكن سداد السلفة كاملة. رصيدك {_fmt_points(result.get('balance', 0))} والسلفة المتبقية {_fmt_points(result.get('loan_debt', 0))}.")
            elif result.get("error") == "no_loan":
                self.send_room_text(room, "ℹ️ لا توجد سلفة قائمة عليك.")
            else:
                self.send_room_text(room, "❌ تعذر سداد السلفة؛ لم يتغير رصيدك.")
            return True
        if game_low in ("سلفني", "loan"):
            result = _request_points_loan(sender_name)
            if result.get("ok"):
                self.send_room_text(
                    room,
                    f"✅ تمت الموافقة على سلفتك: {_fmt_points(LOAN_AMOUNT)} نقطة. "
                    f"رصيدك الآن: {_fmt_points(result.get('balance', 0))}. "
                    f"سيُخصم 20% من مكاسبك حتى سداد {_fmt_points(LOAN_AMOUNT)} نقطة.",
                )
            elif result.get("error") == "balance_too_high":
                self.send_room_text(room, f"❌ لا يمكنك الاستلاف؛ رصيدك {_fmt_points(result.get('balance', 0))} نقطة ويجب أن يكون أقل من 100 مليار.")
            elif result.get("error") == "outstanding_loan":
                self.send_room_text(room, f"⏳ لديك سلفة قائمة متبقية: {_fmt_points(result.get('loan_debt', 0))} نقطة. تُتاح سلفة جديدة بعد سدادها.")
            else:
                self.send_room_text(room, "❌ تعذر تسجيل السلفة الآن؛ لم يتغير رصيدك.")
            return True
        if game_low in ("محفظه", "wallet", "portfolio"):
            return self._wallet_show_catalog(room, sender_name, 1)
        if game_low in ("محفظتي", "my wallet"):
            return self._wallet_show_holdings(room, sender_name, 1)
        if wallet_order:
            return self._wallet_purchase_command(room, sender_name, wallet_order)
        # Keep the user's original mining text: the game normalizer maps "تنقيب"
        # and "احفر" to "الماس", but only the original "تنقيب" joins the timed
        # rare-diamond expedition instead of starting an ordinary dig.
        mining_raw = original_raw
        timed_game_key = TIMED_TREASURE_GAME_ALIASES.get(_normalize_timed_treasure_command(mining_raw))
        if timed_game_key:
            if self._join_rare_diamond_event(room, sender_name, timed_game_key):
                return True
            self.send_room_text(room, "⏳ لا توجد جولة لهذه اللعبة مفتوحة الآن؛ انتظر إعلانها.")
            return True
        mining_low = mining_raw.casefold().replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ى", "ي")
        mining_commands = {
            "الماس", "تنقيب", "نقب", "احفر", "dig", "mine", "mining", "diamond", "diamonds",
            "فحص الماس", "فحص الالماس", "فحص التنقيب", "فحص الماسة النادرة", "فحص الالماسة النادرة", "sv", "مخزني", "مخزون", "مخزون الكنوز",
            "inventory", "my inventory", "bag", "sv inventory", "sv bag", "متجر الكنوز", "سوق الكنوز",
            "treasure shop", "treasure store", "store treasures", "توب الماس", "توب الالماس",
            "top diamonds", "top gems", "اغنى المنقبين", "red",
        }
        if mining_low in mining_commands or re.fullmatch(r"(?:r|rdem)\s+.+", mining_raw, re.I) or re.fullmatch(r"red(?:\s+.+)?", mining_raw, re.I):
            normalized_sender = _norm_user(sender_name)
            user_id = ("name:" + normalized_sender) if normalized_sender else (
                str(sender_name).strip() if str(sender_name).strip().isdigit() else ""
            )
            return self._execute_mining_command_with_id(room, mining_raw, sender_name, user_id)
        if self._handle_bomb_wire(room, sender_name, raw):
            return True
        bomb = re.fullmatch(r"bom@([^@]+)(?:@(.+))?", raw.strip(), re.I)
        if bomb:
            return self._bomb_command(room, sender_name, bomb.group(1), bomb.group(2))
        if game_low in ("عمل", "راتبي"):
            return self._payroll_command(
                room, sender_name, "work" if game_low == "عمل" else "salary"
            )
        if stock_order:
            self.stock_pending.pop(stock_key, None)
            return self._stock_market_invest(room, sender_name, stock_order[0], stock_order[1])
        if game_low in ("زواج", "زوجه"):
            return self._marriage_game(room, sender_name)
        if game_low in ("صيد مزه", "صيد مز"):
            return self._random_social_hunt_game(room, sender_name, game_low)
        if game_low in ("طليقي", "خلع", "طلاق"):
            return self._court_social_game(room, sender_name, game_low)
        if game_low == "زوجتي":
            return self._celebrity_partner_game(room, sender_name, "female")
        if game_low == "زوجي":
            return self._celebrity_partner_game(room, sender_name, "male")
        if game_low in ("خطبه", "خطيب", "حبك", "عدو", "صديق", "زاحف", "خروف"):
            return self._social_pair_game(room, sender_name, game_low)
        if game_low in ("تحدي", "لغز", "حظي", "مزاج"):
            return self._fun_game(room, sender_name, game_low)
        if game_low == "تسليه":
            self.send_room_text(room, "🎉 ألعاب التسلية: زواج، تحدي، لغز، حظي، مزاج")
            return True
        if game_low in ("حصانه", "حصانه!"):
            return self._horse_game(room, sender_name)
        if low.replace("ة", "ه").startswith("زرع"):
            return self._crop_command(room, sender_name, raw)
        if game_low == "شوت":
            return self._fishing_shoot(room, sender_name)
        if low.replace("ة", "ه").startswith("فيس"):
            m=re.fullmatch(r"فيس[@ ](.+)", normalized_raw, re.I)
            return self._fruit_match(room, sender_name, m.group(1).strip() if m else "")
        # New fixed-prize global games. Each one has a single worldwide queue:
        # first verified player opens it, the next verified player joins, then
        # the winner receives +500 and the loser loses 500.
        fixed_games=("سنارة","برق","ياقوت","صدام","كاشف")
        # Use the same ة→ه normalization for canonical names so both
        # سنارة and سناره reach the exact same game handler.
        fixed_lookup={_norm_user(x.replace("ة", "ه")): x for x in fixed_games}
        if _norm_user(game_low) in fixed_lookup:
            game_name=fixed_lookup[_norm_user(game_low)]
            return self._queue_fixed_game(room,sender_name,game_name,500)
        if game_low in ("بورصه", "بورصة", "stock", "market"):
            return self._stock_exchange_game(room, sender_name)
        # PvP games: outcome is decided by strong random selection, never by
        # who entered first or second.
        m=re.fullmatch(r"(مراهنه|رهان|مضاربه|حظي)[@\s]+([0-9][0-9,]*(?:\.[0-9]+)?[kmb]?)", normalized_raw, re.I)
        if m:
            amount = _parse_compact_amount(m.group(2))
            if amount is not None:
                return self._queue_wager(room, sender_name, m.group(1), amount)
        # حظ يا نصيب is the new name for the old stake-based investment game.
        m=re.fullmatch(r"حظ[ _\s]+يا[ _\s]+نصيب[@\s]+([0-9][0-9,]*(?:\.[0-9]+)?[kmb]?)", normalized_raw, re.I)
        if m:
            amount = _parse_compact_amount(m.group(1))
            if amount is not None:
                return self._queue_wager(room, sender_name, "حظ يا نصيب", amount)
        # استثمار@المبلغ هو أمر الاستثمار بالمبلغ، ويحتفظ بنفس نظام الرهان السابق.
        m=re.fullmatch(r"استثمار[@\s]+([0-9][0-9,]*(?:\.[0-9]+)?[kmb]?)", normalized_raw, re.I)
        if m:
            amount = _parse_compact_amount(m.group(1))
            if amount is not None:
                return self._queue_wager(room, sender_name, "استثمار", amount)
        # بورصة@المبلغ ليست لعبة؛ البورصة الآن هي اختيار ذهب/نفط/معادن فقط.
        # كلمة استثمار وحدها ليست لعبة ولا تدخل في فاصل الألعاب.
        m=re.fullmatch(r"حظ[@\s]+([0-9][0-9,]*(?:\.[0-9]+)?[kmb]?)", normalized_raw, re.I)
        if m:
            amount = _parse_compact_amount(m.group(1))
            if amount is not None:
                return self._lottery_game(room, sender_name, amount)
        if game_low == "طاوله":
            return self._table_bot_game(room, sender_name)
        if game_low == "اونو":
            return self._uno_bot_game(room, sender_name)
        if game_low == "عمله":
            return self._coin_bot_game(room, sender_name)
        m = re.fullmatch(r"(?:عملة|عمله)[@ ](وجه|كتابة)", raw, re.I)
        if m:
            return self._coin_bot_game(room, sender_name, m.group(1))
        if game_low == "عجله":
            return self._wheel_bot_game(room, sender_name)
        if low.replace("ة", "ه").startswith("صندوق"):
            return self._box_bot_game(room, sender_name, normalized_raw)
        if low.replace("ة", "ه").startswith("كوب") or low.replace("ة", "ه").startswith("كاس"):
            return self._cup_bot_game(room, sender_name, normalized_raw)
        if low == "وحش":
            return self._monster_bot_game(room, sender_name)
        if low == "بركان":
            return self._volcano_bot_game(room, sender_name)
        if low == "طائر":
            return self._bird_bot_game(room, sender_name)
        if low == "نجم":
            return self._star_bot_game(room, sender_name)
        if game_low in ("بنك مليون", "بنك"):
            return self._million_bank_game(room, sender_name)
        if low in ("مليار","billion"):
            if not self._game_cooldown_notice(room, sender_name, BILLION_GAME_COOLDOWN_SECONDS, "مليار"):
                return True
            english = self._configured_room_language(room) == "en"
            if english:
                self.send_room_text(room, f"🏛️\n{sender_name}\nSearching for a billion\n🏛️")
            else:
                self.send_room_text(room, f"🏛️\n{sender_name}\nجاري البحث عن مليار\n🏛️")
            won = _money_game_roll("billion")
            reward = 1000000000 if won else 0
            _record_game(sender_name, "billion", reward, 0)

            if won:
                _record_money_game_win("billion", sender_name)
                self._game_award(sender_name, reward)
                # Prefer the latest live profile URL; DB lookup is only a fallback.
                winner_key = _norm_user(sender_name)
                winner_photo = self.user_photos.get(winner_key, "")
                if not winner_photo:
                    winner_photo = self._lookup_profile_photo(sender_name)

                target_rooms = self._active_rooms() or [room]
                target_rooms = [room] + [
                    target_room for target_room in target_rooms
                    if _norm_room(target_room) != _norm_room(room)
                ]
                for index, target_room in enumerate(target_rooms):
                    language = self._configured_room_language(target_room)
                    self.send_room_text(target_room, _money_game_win_text("billion", sender_name, language))
                    if index + 1 < len(target_rooms):
                        time.sleep(0.05)

                try:
                    base = _public_base_url()
                    if not base:
                        raise RuntimeError("PUBLIC_MEDIA_URL أو PUBLIC_BASE_URL أو RAILWAY_PUBLIC_DOMAIN غير مضبوط")
                    card = render_billion_card(sender_name, winner_photo)
                    url = f"{base}/billion/{card.name}"
                    self._verify_public_media_url(url, "image")
                    # Publish the same generated winner card to every active room.
                    for target_room in target_rooms:
                        self.send_room_media(target_room, url, "image")
                    self.log("[GAME] billion winner card sent", sender_name, len(target_rooms))
                except Exception as exc:
                    self.log("[GAME] billion winner card failed:", repr(exc))
            else:
                loss_text = (f"🏛️\n{sender_name}\nDid not get the billion\n🏛️"
                             if english else f"🏛️\n{sender_name}\nلم يتم الحصول على مليار\n🏛️")
                self.send_room_text(room, loss_text)
            return True
        if game_low in ("حظ","الحظ","luck"):
            return self._lottery_game(room, sender_name, 0)
        if low in ("حجر","ورق","مقص"):
            if not self._game_cooldown_notice(room, sender_name, 40.0, "حجر_ورق_مقص"):
                return True
            bot_choice=secrets.choice(("حجر","ورق","مقص"))
            win=(low,bot_choice) in (("حجر","مقص"),("ورق","حجر"),("مقص","ورق"))
            if low==bot_choice: result="🤝 تعادل"; reward=0
            elif win: result="🏆 فزت"; reward=200
            else: result="❌ خسرت"; reward=0
            balance=self._game_award(sender_name,reward)
            _record_game(sender_name,"rps",reward,0)
            self.send_room_text(room, f"✂️ @{sender_name}: {low} | 🤖 البوت: {bot_choice}\n{result}\n🎁 +{reward} نقطة\n💰 مبلغ الفوز: +{_fmt_points(reward)} نقطة\n💸 مبلغ الخسارة: 0 نقطة\n💳 الرصيد: {_fmt_points(balance)}")
            return True
        # Steal: random victim with `اسرق`, or a named room member with `اسرق@username` / `اسرق username`.
        m=re.fullmatch(r"اسرق(?:@|\s+@?)([^@\s]+)", raw, re.I)
        if m:
            return self._steal_game(room, sender_name, m.group(1).strip().lstrip("@"))
        if low == "اسرق":
            return self._steal_game(room, sender_name)
        if game_low in ("سرقه"):
            if not self._game_cooldown_notice(room, sender_name, 40.0, "سرقة" if low == "سرقة" else "رشوة"):
                return True
            label = "🕵️ سرقة" if low == "سرقة" else "💼 رشوة"
            won = secrets.randbelow(2) == 0
            reward = secrets.randbelow(101) + 100 if won else 0
            balance = self._game_award(sender_name, reward)
            _record_game(sender_name, "misc", reward, 0)
            self.send_room_text(room, f"{label} @{sender_name}\n" + (f"🏆 نجحت وربحت {reward} نقطة." if won else "❌ لم تنجح هذه المرة.") + f"\n💰 مبلغ الفوز: +{_fmt_points(reward)} نقطة\n💸 مبلغ الخسارة: 0 نقطة\n💳 الرصيد: {_fmt_points(balance)}")
            return True
        return False

    def _send_rare_diamond_teaser(self, room):
        """Send the configured rare-diamond base art with the event announcement."""
        source = _rare_diamond_asset_path()
        if not source:
            self.log("[RARE-DIAMOND] teaser image skipped: rare-diamond artwork has not been added yet")
            return False
        try:
            try:
                relative = source.resolve().relative_to(ASSETS_DIR.resolve())
                parent = relative.parent.as_posix()
                route = "assets" if parent in (".", "") else "assets/" + parent
            except Exception:
                route = "assets/treasures"
            url = self._game_public_image(source, route=route)
            if not url:
                return False
            self._verify_public_media_url(url, "image")
            return self.send_room_media(room, url, "image") is not False
        except Exception as exc:
            self.log("[RARE-DIAMOND] teaser image failed:", repr(exc))
            return False

    def _reserved_stake(self, username, exclude_key=None):
        key = _norm_user(username)
        total = 0
        with self.game_lock:
            for k, waiting in self.wager_waiting.items():
                if exclude_key is not None and k == exclude_key:
                    continue
                if _norm_user(waiting.get("user")) == key:
                    total += int(waiting.get("stake", 0) or 0)
        return total

    def _fruit_match(self, room, sender, emoji):
        if not self._game_cooldown_notice(room, sender, 40.0, "فيس"):
            return True
        fruits=("🍓","🍇","🍉","🍌","🍋","🍊","🍐","🍎","🍏","🥑","🥦","🍑","🥭","🍍","🥥","🥝","🍅","🍆","🧄","🥕","🌽","🌶️")
        if emoji not in fruits:
            self.send_room_text(room, "❌ اختر فاكهة من القائمة: " + " ".join(fruits)); return True
        bot_fruit=random.choice(fruits)
        if emoji == bot_fruit:
            self._send_game_result(room, f"🍉 فيس @{sender}\n✅ تمت المطابقة! البوت أرسل {bot_fruit}\n🏆 فزت بـ 20 نقطة.\n💰 مبلغ الفوز: +20 نقطة\n💸 مبلغ الخسارة: 0 نقطة", "")
            self._game_award(sender,20)
            _record_game(sender, "fruit", 20, 0)
        else:
            self.send_room_text(room, f"🍉 فيس @{sender}\n🤖 البوت أرسل {bot_fruit}\n❌ لم تتم المطابقة، حظاً موفقاً.\n💰 مبلغ الفوز: 0 نقطة\n💸 مبلغ الخسارة: 0 نقطة")
        return True

    def _save_crop_plots(self):
        try:
            with self.game_lock:
                snapshot = {
                    key: {name: value for name, value in plot.items() if name != "_claiming"}
                    if isinstance(plot, dict) else plot
                    for key, plot in self.crop_plots.items()
                }
            _save_local_json(CROP_PLOTS_FILE, snapshot)
            return True
        except Exception as exc:
            self.log("[CROP] save failed:", repr(exc))
            return False

    def _process_due_crop_plots(self, now=None):
        now = time.time() if now is None else float(now)
        ready = []
        with self.game_lock:
            for key, plot in list(getattr(self, "crop_plots", {}).items()):
                try:
                    parts = str(key).split("|", 2)
                    username = str(plot.get("username") or parts[0]).strip().lstrip("@")
                    crop = str(plot.get("crop") or (parts[1] if len(parts) > 1 else "🌱"))
                    finish = float(plot.get("finish", 0))
                    minutes = int(plot.get("minutes", 0))
                    reward = int(plot.get("reward", minutes * 20))
                except Exception:
                    continue
                retry_after = float(getattr(self, "_crop_retry_after", {}).get(key, 0) or 0)
                if now < finish or now < retry_after or plot.get("_claiming"):
                    continue
                if not username or reward <= 0:
                    continue
                plot["_claiming"] = True
                ready.append((key, username, crop, minutes, reward, str(plot.get("room") or "")))

        for key, username, crop, minutes, reward, planted_room in ready:
            try:
                # Use the original saved account name, not the normalized plot
                # key; decorated Talkin names can differ from their storage key.
                balance = self._game_award(username, reward)
            except Exception as exc:
                self.log("[CROP] reward persistence failed; plot retained for retry:", username, repr(exc))
                with self.game_lock:
                    plot = getattr(self, "crop_plots", {}).get(key)
                    if isinstance(plot, dict):
                        plot.pop("_claiming", None)
                    getattr(self, "_crop_retry_after", {})[key] = now + 15.0
                continue

            try:
                _record_game(username, "farm", reward, 0)
            except Exception as exc:
                self.log("[CROP] game statistics save failed:", repr(exc))

            with self.game_lock:
                self.crop_plots.pop(key, None)
                getattr(self, "_crop_retry_after", {}).pop(key, None)
            self._save_crop_plots()

            room = planted_room or str(getattr(self, "room", "") or "")
            if self._configured_room_language(room) == "en":
                result_text = (
                    f"🌾✨ Your harvest is ready!\n━━━━━━━━━━━━\n"
                    f"👤 Player: @{username}\n🌱 Crop: {crop}\n⏱️ Growing time: {minutes} minutes\n"
                    f"💰 Reward: +{_fmt_points(reward)} points\n💸 Loss: 0 points\n"
                    f"💳 Balance now: {_fmt_points(balance)}\n🌟 Plant again whenever you like!"
                )
            else:
                result_text = (
                    f"🌾✨ حصادك جاهز!\n━━━━━━━━━━━━\n"
                    f"👤 اللاعب: @{username}\n🌱 المحصول: {crop}\n⏱️ مدة الزراعة: {minutes} دقيقة\n"
                    f"💰 المكافأة: +{_fmt_points(reward)} نقطة\n💸 الخسارة: 0 نقطة\n"
                    f"💳 رصيدك الآن: {_fmt_points(balance)}\n🌟 ازرع من جديد متى شئت!"
                )
            try:
                private_text = result_text + (
                    f"\n🏠 غرفة الزراعة: {room}" if self._configured_room_language(room) != "en"
                    else f"\n🏠 Farm room: {room}"
                )
                self.send_private_text(username, private_text)
            except Exception as exc:
                self.log("[CROP] harvest credited; private result could not be delivered:", username, repr(exc))
        return len(ready)

    def _crop_options(self):
        return {
            "🍎":(5,50_000), "🍐":(10,75_000), "🍊":(15,100_000), "🍋":(20,150_000),
            "🍇":(25,200_000), "🍉":(30,250_000), "🍓":(35,300_000), "🥕":(40,350_000),
            "🌽":(45,400_000), "🥭":(50,450_000), "🍍":(55,500_000), "🍌":(60,600_000),
            "🥥":(60,700_000), "🥑":(60,850_000), "🥔":(60,1_000_000),
            "🍒":(60,2_000_000), "🍈":(60,5_000_000), "🫐":(60,10_000_000),
            "🍅":(60,25_000_000), "🥒":(60,50_000_000), "🧄":(60,100_000_000),
            "🌻":(60,200_000_000), "🌾":(60,300_000_000), "🌳":(60,500_000_000),
        }

    def _crop_worker(self):
        while not self.stop_event.is_set():
            try:
                self._process_due_crop_plots()
            except Exception as exc:
                self.log("[CROP] worker iteration failed; worker will continue:", repr(exc))
            self.stop_event.wait(2.0)

    def _crop_command(self, room, sender, raw):
        crops = self._crop_options()
        english = self._effective_room_language(room) == "en"
        if raw.casefold()=="زرع":
            crop_lines = [
                (f"{item} {minutes}m={reward:,}" if english
                 else f"{item} {minutes}د={_fmt_points(reward)}")
                for item, (minutes, reward) in crops.items()
            ]
            page_size = 30
            pages = [crop_lines[index:index + page_size] for index in range(0, len(crop_lines), page_size)] or [[]]
            page_texts = []
            for index, page in enumerate(pages, 1):
                heading = (
                    f"🌱 Crop menu — minutes/reward points — List {index}/{len(pages)}"
                    if english else f"🌱 قائمة المحاصيل — الوقت بالدقائق/المكافأة بالنقاط — القائمة {index}/{len(pages)}"
                )
                footer = (
                    "📌 Plant: farm@🍎 or farm 🍎"
                    if english else "📌 للزراعة: زرع@🍎 أو زرع 🍎"
                )
                page_texts.append(heading + "\n" + "\n".join(page) + "\n" + footer)
            self._send_room_result_pages(room, sender, page_texts, "en" if english else "ar")
            return True
        m=re.fullmatch(r"زرع[@ ](.+)", raw, re.I)
        if not m: return False
        crop=m.group(1).strip()
        if crop not in crops:
            self.send_room_text(room, "❌ Choose a crop from the farm list." if english else "❌ اختر محصولاً من قائمة زرع.")
            return True
        user_key=_norm_user(sender)
        duplicate_crop_text = (
            "🌱 Choose a different crop; the same crop cannot be planted twice at once."
            if english else "🌱 ازرع محصولاً آخر؛ لا يمكن زراعة نفس النوع مرتين في الوقت نفسه."
        )
        limit_text = (
            "⏳ You already have 5 crops growing. Harvest one before planting another."
            if english else "⏳ لديك 5 محاصيل قيد الزراعة حالياً. احصد أحدها أولاً ثم ازرع محصولاً جديداً."
        )
        with self.game_lock:
            # يسمح لكل مستخدم بحد أقصى 5 محاصيل نشطة في الوقت نفسه،
            # وكل نوع محصول مرة واحدة فقط؛ تكرار نفس النوع يكون صامتاً.
            active_user = []
            active_crops = set()
            for key, plot in self.crop_plots.items():
                if key.startswith(user_key + "|"):
                    active_user.append((key, plot))
                    active_crops.add(str(plot.get("crop") or ""))

            if crop in active_crops:
                self.send_room_text(room, duplicate_crop_text)
                return True

            if len(active_user) >= 5:
                self.send_room_text(room, limit_text)
                return True

        if not self._game_cooldown_notice(room, sender, 40.0, "زرع"):
            return True

        with self.game_lock:
            # نعيد الفحص بعد الانتظار لتجنب التكرار إذا وصلت زراعة أخرى بالتزامن.
            active_user = []
            active_crops = set()
            for key, plot in self.crop_plots.items():
                if key.startswith(user_key + "|"):
                    active_user.append((key, plot))
                    active_crops.add(str(plot.get("crop") or ""))
            if crop in active_crops:
                self.send_room_text(room, duplicate_crop_text)
                return True
            if len(active_user) >= 5:
                self.send_room_text(room, limit_text)
                return True

            minutes,reward=crops[crop]
            slot=uuid.uuid4().hex[:8]
            key=f"{user_key}|{crop}|{slot}"
            self.crop_plots[key]={
                "username":str(sender).strip().lstrip("@"),
                "crop":crop, "minutes":minutes, "reward":reward,
                "finish":time.time()+minutes*60, "room":str(room or "")
            }
        self._save_crop_plots()
        if english:
            message = (
                f"🌱 Planted {crop} successfully!\n⏱️ Growing time: {minutes} minutes\n"
                f"🎁 Reward: {reward:,} points\n📩 The harvest result will be sent to your private messages when ready."
            )
        else:
            message = (
                f"🌱 تم زرع {crop} بنجاح!\n⏱️ الانتظار: {minutes} دقيقة\n"
                f"🎁 المكافأة: {reward} نقطة\n📩 عند اكتمالها ستصلك النتيجة في الخاص."
            )
        self.send_room_text(room, message)
        return True

    def _bomb_command(self, room, sender, target, target_room=None):
        """Start a room bomb challenge; only a room manager may trigger a kick-based round."""
        source_room = str(room or "").strip()
        destination = str(target_room or source_room).strip()
        target = str(target or "").strip().lstrip("@")
        english = getattr(self, "_configured_room_language", lambda _room: "ar")(source_room) == "en"
        if not target or not destination:
            self.send_room_text(source_room, "❌ Format: bom@username or bom@username@room" if english else "❌ الصيغة: bom@اسم_المستخدم أو bom@اسم_المستخدم@اسم_الغرفة")
            return True
        if not _room_manager(self, source_room, sender):
            self.send_room_text(source_room, "🔒 Only the master or room staff can start a bomb challenge because a wrong wire causes a kick." if english else "🔒 بدء القنبلة التي قد تؤدي إلى طرد لاعب مخصص للماستر أو إدارة الغرفة.")
            return True
        if _norm_user(target) == _norm_user(sender):
            self.send_room_text(source_room, "❌ You cannot target yourself." if english else "❌ لا يمكنك اختيار نفسك هدفًا للقنبلة.")
            return True
        if member_shop.active("user_bomb", username=target,
                              user_id=_stored_user_id(target, destination),
                              aliases=_stored_user_aliases(target, destination)):
            self.send_room_text(source_room, f"🛡️ @{target} محمي من القنابل حالياً.")
            return True
        if member_shop.active("room_bomb", room=destination):
            self.send_room_text(source_room, f"🏰 الغرفة {destination} محمية من القنابل حالياً.")
            return True
        active_rooms = {_norm_room(r): str(r).strip() for r in self._active_rooms() if str(r).strip()}
        if _norm_room(destination) not in active_rooms:
            self.send_room_text(source_room, f"❌ The bot is not connected to {destination}." if english else f"❌ البوت غير متصل بالغرفة {destination}.")
            return True
        destination = active_rooms[_norm_room(destination)]
        # Only start a challenge for a user that is present in the target
        # room's live roster. This prevents sending a bomb to stale/saved
        # usernames that are no longer in the room.
        destination_roster = None
        for known_room, roster in (getattr(self, "room_users", {}) or {}).items():
            if _norm_room(known_room) == _norm_room(destination):
                destination_roster = roster if isinstance(roster, dict) else {}
                break
        present = bool(destination_roster) and any(
            _norm_user(username) == _norm_user(target)
            for username in destination_roster
        )
        # The in-memory roster can lag after reconnect. Ask the configured
        # database roster before declaring a user absent; the old check caused
        # false "not present" replies when the bot was already in the room.
        if not present and getattr(self, "db", None):
            try:
                db_rows = self.db.room_users(destination) or []
                for row in db_rows:
                    username = str(row.get("username") or "").strip() if isinstance(row, dict) else ""
                    if username:
                        self.room_users.setdefault(destination, {})[username] = str(row.get("role") or "none")
                    if username and _norm_user(username) == _norm_user(target):
                        present = True
            except Exception as exc:
                self.log("[BOMB] database presence lookup failed:", destination, repr(exc))
        if not present:
            self.send_room_text(
                source_room,
                f"❌ User {target} is not present in room {destination}."
                if english else f"❌ المستخدم @{target} غير موجود في الغرفة {destination}."
            )
            return True
        lock = getattr(self, "_bomb_games_lock", None)
        if lock is None:
            self._bomb_games_lock = lock = threading.RLock()
        key = (_norm_room(destination), _norm_user(target))
        with lock:
            games = getattr(self, "bomb_games", {})
            if key in games and float(games[key].get("expires_at", 0)) > time.time():
                self.send_room_text(source_room, f"⏳ @{target} already has an active bomb challenge in {destination}." if english else f"⏳ لدى @{target} قنبلة نشطة بالفعل في {destination}.")
                return True
            games[key] = {
                "room": destination, "target": target, "started_by": str(sender),
                "source_room": source_room,
                "wire": secrets.randbelow(3) + 1, "expires_at": time.time() + 30.0,
                "id": uuid.uuid4().hex, "defuse_reward": secrets.choice((500_000, 1_000_000)),
            }
            expires_at = float(games[key]["expires_at"])
            self.bomb_games = games
        target_english = getattr(self, "_configured_room_language", lambda _room: "ar")(destination) == "en"
        target_plain, sender_plain = target.lstrip("@"), str(sender).lstrip("@")
        challenge = (
            f"💣 A bomb was sent to {target_plain} by {sender_plain}!\nChoose a wire to defuse it:\n1️⃣ 🔴 Red\n2️⃣ 🟡 Yellow\n3️⃣ 🟢 Green\n⏱️ You have 30 seconds; send one number.\n🏆 Successfully defuse it to win 500,000 or 1,000,000 points."
            if target_english else
            f"💣 وصلت قنبلة إلى {target_plain} من {sender_plain}!\nلتفكيكها اختر رقم السلك:\n1️⃣ 🔴 أحمر\n2️⃣ 🟡 أصفر\n3️⃣ 🟢 أخضر\n⏱️ أمامك 30 ثانية؛ أرسل رقمًا واحدًا.\n🏆 إذا نجحت في التفكيك ستحصل على جائزة 500 ألف أو مليون نقطة."
        )
        try:
            send_result = self.send_room_text(destination, challenge)
        except Exception as exc:
            self.log("[BOMB] challenge send failed:", destination, repr(exc))
            send_result = False
        if send_result is False:
            with lock:
                current = getattr(self, "bomb_games", {}).get(key)
                if (isinstance(current, dict)
                        and current.get("source_room") == source_room
                        and current.get("started_by") == str(sender)):
                    self.bomb_games.pop(key, None)
            source_english = getattr(self, "_configured_room_language", lambda _room: "ar")(source_room) == "en"
            failed = (
                f"❌ Failed to send the bomb challenge to {target_plain} in {destination}."
                if source_english else
                f"❌ فشل إرسال القنبلة إلى {target_plain} في غرفة {destination}."
            )
            self.send_room_text(source_room, failed)
            return True
        self._schedule_bomb_expiry(key, expires_at)
        if _norm_room(source_room) != _norm_room(destination):
            sent_notice = (
                f"✅ Bomb challenge sent to {target_plain} in {destination}."
                if english else
                f"✅ تم إرسال القنبلة إلى {target_plain} في غرفة {destination}."
            )
            self.send_room_text(source_room, sent_notice)
        private_notice = (
            f"💣 {sender_plain} sent you a bomb in {destination}. Go to that room and defuse it within 30 seconds by sending 1, 2, or 3. Successfully defuse it to win 500,000 or 1,000,000 points."
            if target_english else
            f"💣 أرسل لك {sender_plain} قنبلة في غرفة {destination}. اذهب إلى الغرفة وفككها خلال 30 ثانية بإرسال رقم السلك 1 أو 2 أو 3. إذا نجحت في التفكيك ستحصل على جائزة 500 ألف أو مليون نقطة."
        )
        try:
            self.send_private_text(target_plain, private_notice)
        except Exception as exc:
            self.log("[BOMB] private notice failed:", target_plain, destination, repr(exc))
        return True

    def _schedule_bomb_expiry(self, key, expires_at):
        """Schedule a non-blocking timeout; wrong/no answer triggers a kick request."""
        delay = max(0.0, float(expires_at) - time.time())
        timer = threading.Timer(delay, self._expire_bomb_challenge, args=(key, float(expires_at)))
        timer.daemon = True
        timer.start()

    def _expire_bomb_challenge(self, key, expected_expires_at):
        lock = getattr(self, "_bomb_games_lock", None)
        if lock is None:
            return False
        with lock:
            game = getattr(self, "bomb_games", {}).get(key)
            if not isinstance(game, dict):
                return False
            if abs(float(game.get("expires_at", 0) or 0) - float(expected_expires_at)) > 0.001:
                return False
            remaining = float(expected_expires_at) - time.time()
            if remaining > 0:
                self._schedule_bomb_expiry(key, expected_expires_at)
                return False
            self.bomb_games.pop(key, None)
        self._finish_expired_bomb(game)
        return True

    def _finish_expired_bomb(self, game):
        """Kick a target who did not answer within 30 seconds and notify both rooms."""
        destination = str(game.get("room") or "").strip()
        source_room = str(game.get("source_room") or destination).strip()
        target = str(game.get("target") or "").strip().lstrip("@")
        try:
            kick_sent = self.request_admin_action(
                destination, target, "kick", str(game.get("started_by") or "bomb timeout"), announce_room=True
            )
        except Exception as exc:
            self.log("[BOMB] timeout kick failed:", destination, target, repr(exc))
            kick_sent = False
        rooms, seen = [], set()
        for room in (destination, source_room):
            norm = _norm_room(room)
            if room and norm not in seen:
                seen.add(norm)
                rooms.append(room)
        for room in rooms:
            english = getattr(self, "_configured_room_language", lambda _room: "ar")(room) == "en"
            prefix = ""
            if _norm_room(room) != _norm_room(destination):
                prefix = f"📍 Room: {destination}\n" if english else f"📍 الغرفة: {destination}\n"
            if english:
                action = "The kick request was sent." if kick_sent else "The kick request failed."
                message = f"💥 {target} did not send a wire number within 30 seconds; the bomb exploded. {action}"
            else:
                action = "تم إرسال طلب الطرد." if kick_sent else "تعذر إرسال طلب الطرد."
                message = f"💥 لم يرسل {target} رقم السلك خلال 30 ثانية؛ انفجرت القنبلة. {action}"
            try:
                self.send_room_text(room, prefix + message)
            except Exception as exc:
                self.log("[BOMB] timeout result notice failed:", room, repr(exc))

    def _handle_bomb_wire(self, room, username, text):
        digits = str(text or "").strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
        if digits not in {"1", "2", "3"}:
            return False
        key = (_norm_room(room), _norm_user(username))
        lock = getattr(self, "_bomb_games_lock", None)
        if lock is None:
            return False
        with lock:
            game = getattr(self, "bomb_games", {}).get(key)
            if not isinstance(game, dict):
                return False
            self.bomb_games.pop(key, None)
        destination = str(game.get("room") or room).strip()
        source_room = str(game.get("source_room") or destination).strip()
        result_rooms, seen = [], set()
        for result_room in (destination, source_room):
            normalized_room = _norm_room(result_room)
            if result_room and normalized_room not in seen:
                seen.add(normalized_room)
                result_rooms.append(result_room)

        def send_result(kind, extra=""):
            for result_room in result_rooms:
                english = getattr(self, "_configured_room_language", lambda _room: "ar")(result_room) == "en"
                prefix = ""
                if _norm_room(result_room) != _norm_room(destination):
                    prefix = f"📍 Room: {destination}\n" if english else f"📍 الغرفة: {destination}\n"
                if kind == "defused":
                    if str(extra).startswith("reward:"):
                        reward = int(str(extra).split(":", 1)[1])
                        message = (
                            f"✅ {username} successfully defused the bomb and received +{_fmt_points(reward)} points!"
                            if english else f"✅ نجح {username} في تفكيك القنبلة وحصل على +{_fmt_points(reward)} نقطة!"
                        )
                    else:
                        message = (
                            f"✅ {username} defused the bomb, but the reward could not be saved; contact the master."
                            if english else f"✅ نجح {username} في تفكيك القنبلة، لكن تعذر حفظ الجائزة؛ أبلغ الماستر."
                        )
                else:
                    action_result = (
                        "The room kick request was sent." if extra == "kick_sent"
                        else "The room kick request failed."
                    ) if english else (
                        "تم إرسال طلب الطرد." if extra == "kick_sent"
                        else "فشل إرسال طلب الطرد."
                    )
                    message = (
                        f"💥 The bomb exploded on {username} in {destination}; {action_result}"
                        if english else f"💥 انفجرت القنبلة على {username} في غرفة {destination}؛ {action_result}"
                    )
                self.send_room_text(result_room, prefix + message)

        if time.time() >= float(game.get("expires_at", 0)):
            self._finish_expired_bomb(game)
        elif int(digits) == int(game.get("wire") or 0):
            target = str(game.get("target") or username).strip().lstrip("@")
            reward = int(game.get("defuse_reward") or secrets.choice((500_000, 1_000_000)))
            transaction_id = f"bomb-defuse-reward:{game.get('id') or _norm_room(destination) + ':' + _norm_user(target) + ':' + str(int(game.get('expires_at', 0)))}"
            award = _apply_points_delta_once(
                target, reward, transaction_id, "bomb defusal reward", repay_loan=True
            )
            self._notify_loan_repayment(target, award)
            if award.get("ok"):
                try:
                    _record_game(target, "bomb_defuse", reward, 0)
                except Exception as exc:
                    self.log("[BOMB] defusal statistics save failed:", repr(exc))
                send_result("defused", f"reward:{reward}")
            else:
                self.log("[BOMB] defusal reward persistence failed:", target, award.get("error"))
                send_result("defused", "reward_failed")
        else:
            colors = {"1": ("الأحمر", "Red"), "2": ("الأصفر", "Yellow"), "3": ("الأخضر", "Green")}
            color = colors[digits][0]
            try:
                kick_sent = self.request_admin_action(
                    room, username, "kick", str(game.get("started_by") or "bomb game"), announce_room=True
                )
            except Exception as exc:
                self.log("[BOMB] kick failed:", room, username, repr(exc))
                kick_sent = False
            send_result("exploded", "kick_sent" if kick_sent else "kick_failed")
        return True

    def _royal_hunt_event_worker(self):
        while not self.stop_event.wait(360.0):
            try:
                self._start_royal_hunt_event()
            except Exception as exc:
                self.log("[ROYAL-HUNT] scheduler failed:", repr(exc))

    def _start_royal_hunt_event(self, item=None, announce=True):
        now = time.time()
        with self._royal_hunt_lock:
            item = item or getattr(self, "_royal_hunt_next_kind", "royal_diamond")
            item = "royal_dragon" if item == "royal_dragon" else "royal_diamond"
            self._royal_hunt_next_kind = "royal_dragon" if item == "royal_diamond" else "royal_diamond"
            self._royal_hunt_round_seq += 1
            self._royal_hunt_event = {
                "id": self._royal_hunt_round_seq, "kind": item,
                "ends_at": now + 180.0, "winner": "", "announced": False,
            }
            event = dict(self._royal_hunt_event)
        if not announce:
            return event
        rooms = sorted({str(r).strip() for r in (self._active_rooms() or []) if str(r).strip()}, key=str.casefold)
        opening_barrier = threading.Barrier(len(rooms)) if rooms else None
        def announce_room(target):
            if item == "royal_dragon":
                text = "🏹 الجولة الملكية: سهم التنين 🐉\nظهر التنين الملكي! الجولة مفتوحة 3 دقائق.\nأرسل «سهم» فقط للمشاركة والفوز بـ 2 مليار نقطة."
            else:
                text = "💎 الجولة الملكية: صيد الألماسة\nظهرت الألماسة الملكية! الجولة مفتوحة 3 دقائق.\nأرسل «الماسه» فقط للمشاركة والفوز بـ 4 مليارات نقطة."
            # Text-only round announcement; winner cards are sent only after a win.
            try:
                if opening_barrier is not None:
                    opening_barrier.wait(timeout=10.0)
                with self._royal_hunt_lock:
                    current = getattr(self, "_royal_hunt_event", None)
                    if (
                        not isinstance(current, dict)
                        or current.get("id") != event.get("id")
                        or current.get("winner")
                        or current.get("opening_cancelled")
                    ):
                        return
                self._send_game_open_announcement(target, text)
            except Exception as exc: self.log("[ROYAL-HUNT] announce failed:", target, repr(exc))
        if rooms:
            # Launch one send per room concurrently. This minimizes room-to-room
            # skew and keeps every room's opening notice ahead of any winner result.
            with ThreadPoolExecutor(max_workers=max(1, len(rooms)), thread_name_prefix="royal-open") as pool:
                list(pool.map(announce_room, rooms))
        with self._royal_hunt_lock:
            current = self._royal_hunt_event
            if isinstance(current, dict) and current.get("id") == event.get("id"):
                current["announced"] = True
                current["opening_complete_at"] = time.monotonic()
        self.log("[ROYAL-HUNT] round announcement barrier released", event.get("id"), "rooms=", len(rooms))
        return event

    def _royal_hunt_command(self, room, sender, raw):
        text = " ".join(str(raw or "").strip().casefold().replace("أ", "ا").replace("إ", "ا").split()).replace("ة", "ه")
        if text in {"صيدي", "صيدى", "my hunt"}:
            inv = self._royal_hunt.inventory(sender, sender)
            animals = inv.get("animals", {}) if isinstance(inv.get("animals"), dict) else {}
            shown = ", ".join(f"{k} ×{v}" for k,v in animals.items()) if animals else "لا يوجد"
            self.send_room_text(room, f"🏹 صيد @{str(sender).lstrip('@')}\n🐉 تنانين ملكية: {int(inv.get('dragon', 0) or 0)}\n💎 ألماسات ملكية: {int(inv.get('diamond', 0) or 0)}\n🐾 الحيوانات: {shown}")
            return True
        if text in {"ماستي", "ماسهتي", "my royal diamond"}:
            inv = self._royal_hunt.inventory(sender, sender)
            self.send_room_text(room, f"💎 لديك {int(inv.get('diamond', 0) or 0)} ألماسة ملكية فاخرة — قيمة الواحدة {_fmt_points(ROYAL_DIAMOND_VALUE)} نقطة.")
            return True
        if text in {"توب الماسه", "top diamond", "top diamonds"}:
            rows = self._royal_hunt.top_diamonds(10)
            lines = [f"{i}. @{name} — 💎 {count}" for i, (count, name) in enumerate(rows, 1)]
            self.send_room_text(room, "🏆 توب الألماسة الملكية\n" + ("\n".join(lines) if lines else "لا توجد نتائج بعد."))
            return True
        if text in {"توب شوت", "top shot", "top royal dragon"}:
            rows = self._royal_hunt.top_dragons(10)
            lines = [f"{i}. @{name} — 🐉 {count}" for i, (count, name) in enumerate(rows, 1)]
            self.send_room_text(room, "🏆 توب صائدي التنين الملكي\n" + ("\n".join(lines) if lines else "لا توجد نتائج بعد."))
            return True
        if text in {"الماسي", "الماسي", "top royal", "royal top"}:
            rows = self._royal_hunt.top(10)
            lines = [f"{i}. @{name} — 💎 {diamond} | 🐉 {dragon}" for i,(diamond,dragon,name) in enumerate(rows, 1)]
            self.send_room_text(room, "🏆 توب الألماسة والتنين الملكي\n" + ("\n".join(lines) if lines else "لا توجد نتائج بعد."))
            return True
        # Royal inventory sales accept Arabic/English aliases and both
        # بيع@الماس الملكي@2 and بيع الماس الملكي 2 forms.
        sell = re.fullmatch(
            r"(?:بيع|sell)\s*@?\s*(.+?)\s*(?:@|×|x|\s)\s*([0-9٠-٩۰-۹]+)",
            text,
            re.I,
        )
        if sell:
            label = " ".join(sell.group(1).strip().split())
            amount_text = sell.group(2).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
            amount = int(amount_text)
            diamond_aliases = {
                "ماس", "الماس", "الماسه", "الماس الملكي", "الماس الملكيه",
                "الماسه الملكيه", "الالماس الملكي", "الالماسه الملكيه",
                "الماسه الملكي", "diamond", "royal diamond", "ملكيه", "الملكيه",
            }
            dragon_aliases = {
                "تنين", "التنين", "تنين ملكي", "التنين الملكي", "تنين الملكي",
                "dragon", "royal", "royal dragon", "الملكي",
            }
            if label not in diamond_aliases and label not in dragon_aliases:
                self.send_room_text(room, "❌ نوع غير معروف. استخدم: بيع@الماس الملكي@العدد أو بيع@التنين الملكي@العدد.")
                return True
            is_diamond = label in diamond_aliases
            if amount < 1:
                self.send_room_text(room, "❌ أدخل كمية صحيحة أكبر من صفر.")
                return True
            item_key = "royal_diamond" if is_diamond else "royal_dragon"
            result = self._royal_hunt.sell(sender, sender, item_key, amount)
            if not result.get("ok"):
                self.send_room_text(room, "❌ لا تملك الكمية المطلوبة للبيع. اكتب «صيدي» لمعرفة مخزونك.")
            else:
                _add_points(sender, result["value"], repay_loan=True)
                item_name = "الألماسة الملكية" if is_diamond else "التنين الملكي"
                self.send_room_text(room, f"✅ تم بيع {result['sold']} من {item_name} مقابل +{_fmt_points(result['value'])} نقطة.")
            return True
        if text in {"سهم", "arrow", "صيد التنين الملكي", "صيد تنين ملكي", "سهم ملكي", "سهم التنين الملكي", "royal dragon hunt", "hunt royal dragon"}:
            # Arrow is reserved for the royal dragon; Shot remains the ordinary hunt.
            return self._royal_hunt_claim(room, sender, "royal_dragon")
        if text in {"الماسه", "diamond", "صيد الماسه", "صيد الماس", "الماس الملكي", "royal diamond hunt", "hunt royal diamond"}:
            return self._royal_hunt_claim(room, sender, "royal_diamond")
        return False

    def _royal_hunt_claim(self, room, sender, item):
        now = time.time()
        with self._royal_hunt_lock:
            event = self._royal_hunt_event
            if not isinstance(event, dict) or now >= float(event.get("ends_at", 0) or 0):
                target = "التنين الملكي" if item == "royal_dragon" else "الألماسة الملكية"
                self.send_room_text(room, f"❌ لم تعثر على {target}؛ لا توجد جولة معلنة الآن.")
                return True
            if not event.get("announced", True):
                self.send_room_text(room, "⏳ يجري نشر إعلان الجولة في جميع الغرف؛ أعد المحاولة بعد لحظة.")
                return True
            field = "dragon" if item == "royal_dragon" else "diamond"
            if event.get("kind") != item:
                current = "التنين الملكي" if event.get("kind") == "royal_dragon" else "الألماسة الملكية"
                target = "التنين الملكي" if item == "royal_dragon" else "الألماسة الملكية"
                self.send_room_text(room, f"❌ لم تعثر على {target}؛ الجولة الحالية مخصصة لـ {current}.")
                return True
            if event.get("winner"):
                target = "التنين الملكي" if item == "royal_dragon" else "الألماسة الملكية"
                self.send_room_text(room, f"🏹 رميت {'سهمًا' if item == 'royal_dragon' else 'الماسة'} ولم تعثر على {target}." )
                return True
            event_id = event.get("id")
            event["winner"] = str(sender)
        try:
            threading.Thread(
                target=self._royal_hunt_winner_worker,
                args=(room, sender, item, event_id),
                name="royal-hunt-winner",
                daemon=True,
            ).start()
        except Exception as exc:
            self.log("[ROYAL-HUNT] winner worker start failed:", repr(exc))
            self.send_room_text(room, "⚠️ تم تسجيل الفوز، لكن تعذر بدء تجهيز بطاقة الفوز. أبلغ الماستر.")
        return True

    def _royal_hunt_winner_worker(self, room, sender, item, event_id=None):
        """Award the winner and fan out the winner card away from the receive loop."""
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with self._royal_hunt_lock:
                current = getattr(self, "_royal_hunt_event", None)
                if not event_id or not isinstance(current, dict) or current.get("id") != event_id:
                    break
                completed_at = float(current.get("opening_complete_at", 0) or 0)
                if completed_at:
                    remaining = 0.35 - (time.monotonic() - completed_at)
                    if remaining > 0:
                        time.sleep(min(remaining, 0.05))
                        continue
                    break
            time.sleep(0.02)
        try:
            self._royal_hunt.add(sender, sender, item, room)
            value = ROYAL_DRAGON_VALUE if item == "royal_dragon" else ROYAL_DIAMOND_VALUE
            _add_points(sender, value, repay_loan=True)
            base = _public_base_url()
            label = "التنين الملكي 🐉" if item == "royal_dragon" else "الألماسة الملكية 💎"
            avatar_url = getattr(self, "user_photos", {}).get(_norm_user(sender), "") or self._lookup_profile_photo(sender)
            winner_card = self._render_royal_winner_card(item, sender, avatar_url)
            optimized = _optimize_social_media_asset(winner_card) if winner_card else None
            media = f"{base}/publish/{quote(Path(optimized).name)}" if base and optimized else ""
            message = f"🏆 @{str(sender).lstrip('@')} اصطاد {label}!\n💰 الجائزة: +{_fmt_points(value)} نقطة\n📷 اسم الفائز وصورته داخل بطاقة الفوز."
            rooms = sorted({str(r).strip() for r in (self._active_rooms() or []) if str(r).strip()} | {str(room).strip()})
            def deliver(target):
                try:
                    self.send_room_text(target, message)
                    if media:
                        self.send_room_media(target, media, "image")
                except Exception as exc:
                    self.log("[ROYAL-HUNT] result failed:", target, repr(exc))
            if rooms:
                with ThreadPoolExecutor(max_workers=max(1, len(rooms)), thread_name_prefix="royal-result") as pool:
                    list(pool.map(deliver, rooms))
        except Exception as exc:
            self.log("[ROYAL-HUNT] winner processing failed:", repr(exc))
            try:
                self.send_room_text(room, "❌ تعذر إكمال احتساب أو إعلان الفوز؛ راجع سجل البوت.")
            except Exception:
                pass

    def _live_room_user_roles(self, room):
        """Read the in-memory room roster only; never consult bot-wide member files."""
        room_key = _norm_room(room).casefold()
        live = {}
        if not room_key:
            return live
        for cached_room, cached_users in getattr(self, "room_users", {}).items():
            if _norm_room(cached_room).casefold() != room_key:
                continue
            if isinstance(cached_users, dict):
                live.update(cached_users)
            elif isinstance(cached_users, (list, tuple, set)):
                live.update({str(name): "member" for name in cached_users})
        return live

    def _room_member_usernames_for_steal(self, room, exclude_username=""):
        """Return only eligible usernames from this room's live in-memory roster."""
        excluded = {_norm_user(exclude_username), _norm_user(BOT_ID)}
        candidates = []
        seen = set()
        live = self._live_room_user_roles(room)
        for username, role in live.items():
            u = str(username or "").strip().lstrip("@").strip()
            key = _norm_user(u)
            if not u or not key or key in excluded or key in seen:
                continue
            if str(role or "").casefold().strip() in {"outcast", "banned", "ban", "blocked"}:
                continue
            # Do not allow stealing from the configured master.
            if _is_master_name(u):
                continue
            seen.add(key)
            candidates.append(u)
        return candidates

    def _steal_protected(self, username):
        key = _norm_user(username)
        if not key:
            return False
        expires = float(self.steal_protection.get(key, 0) or 0)
        if expires <= time.time():
            self.steal_protection.pop(key, None)
            return False
        return True

    def _bot_win_reward(self):
        """Random winning reward for normal bot-vs-player games."""
        return secrets.randbelow(4001) + 1000  # 1,000..5,000

    def _handle_pending_bot_choice(self, room, text, sender_name):
        """Resolve number-only replies for عملة/صندوق choice prompts."""
        raw = str(text or "").strip()
        if not raw or not sender_name:
            return False
        digit_map = {
            "1": 1, "2": 2, "3": 3,
            "١": 1, "٢": 2, "٣": 3,
            "\u20661.\u2069": 1, "\u20662.\u2069": 2, "\u20663.\u2069": 3,
            "🟦1": 1, "🟦2": 2, "🟦3": 3,
            "🟦\u20661.\u2069": 1, "🟦\u20662.\u2069": 2, "🟦\u20663.\u2069": 3,
        }
        choice = digit_map.get(raw)
        if choice is None:
            return False
        key = _game_choice_key(room, sender_name)
        pending = self.pending_bot_choices.get(key)
        if not isinstance(pending, dict):
            return False
        if time.time() - float(pending.get("created", 0) or 0) > 120:
            self.pending_bot_choices.pop(key, None)
            self.send_room_text(room, f"⏰ @{sender_name} انتهى وقت الاختيار. أرسل أمر اللعبة من جديد.")
            return True
        game = pending.get("game")
        if game == "coin":
            if choice not in (1, 2):
                self.send_room_text(room, "❌ اختر 1 أو 2 فقط.\n\u20661.\u2069 وجه\n\u20662.\u2069 كتابة")
                return True
            result = pending.get("result") or secrets.choice(("وجه", "كتابة"))
            selected = "وجه" if choice == 1 else "كتابة"
            won = selected == result
            reward = self._bot_win_reward() if won else 0
            self.pending_bot_choices.pop(key, None)
            balance = self._game_award(sender_name, reward)
            _record_game(sender_name, "coin", reward, 0)
            self.send_room_text(room, f"🪙 لعبة العملة\n━━━━━━━━━━━━━━\n@{sender_name}\n🎯 اختيارك: {selected}\n🪙 النتيجة: {result}\n{('🏆 فزت!' if won else '❌ لم تفز هذه المرة.')}\n🎁 +{_fmt_points(reward)} نقطة\n💰 رصيدك: {_fmt_points(balance)}")
            return True
        if game == "box":
            if choice not in (1, 2, 3):
                self.send_room_text(room, "❌ اختر 1 أو 2 أو 3 فقط.")
                return True
            prize_box = int(pending.get("prize_box") or 1)
            reward = int(pending.get("reward") or 0) if choice == prize_box else 0
            self.pending_bot_choices.pop(key, None)
            balance = self._game_award(sender_name, reward)
            _record_game(sender_name, "box", reward, 0)
            body = (f"📦 اخترت الصندوق {choice}\n🏆 الصندوق الرابح: {prize_box}\n✅ ربحت!" if reward else f"📦 اخترت الصندوق {choice}\n🎲 الصندوق الرابح كان: {prize_box}\n❌ لم تربح.")
            self.send_room_text(room, f"📦 لعبة الصناديق\n━━━━━━━━━━━━━━\n@{sender_name}\n{body}\n🎁 +{_fmt_points(reward)} نقطة\n💰 رصيدك: {_fmt_points(balance)}")
            return True
        return False

    def _fun_room_members(self, room, sender):
        """Return eligible members of this room from the live cache only."""
        users = self._live_room_user_roles(room)
        out=[]
        excluded = {_norm_user(sender), _norm_user(BOT_ID), _norm_user("🤖 البوت")}
        for name, role in users.items():
            clean=str(name or "").strip().lstrip("@")
            role_key = str(role or "").casefold().strip()
            if (clean and _norm_user(clean) not in excluded
                    and role_key not in {"outcast", "banned", "ban", "blocked", "kicked"}):
                out.append(clean)
        return out

    def _rare_diamond_event_worker(self):
        """Run one round at a time; pause ten minutes after it closes."""
        sequence = list(TIMED_TREASURE_GAMES)
        next_index = int(getattr(self, "_treasure_cycle_index", 0) or 0) % len(sequence)
        next_start = float(getattr(self, "_rare_diamond_next_due", time.time()))
        while not self.stop_event.wait(1.0):
            now = time.time()
            if next_start == float("inf"):
                next_start = float(getattr(
                    self, "_rare_diamond_next_due",
                    now + TIMED_TREASURE_ANNOUNCEMENT_INTERVAL_SECONDS,
                ))
            with self._rare_diamond_lock:
                event = self._rare_diamond_event
                expired = isinstance(event, dict) and now >= float(event.get("ends_at", 0) or 0)
            if expired:
                with self._rare_diamond_lock:
                    if self._rare_diamond_event is event:
                        self._rare_diamond_event = None
                        self._rare_diamond_next_due = time.time() + TIMED_TREASURE_ANNOUNCEMENT_INTERVAL_SECONDS
                required = int(event.get("required_players", 5))
                for target_room in event.get("rooms", [event.get("room", "")]):
                    try:
                        game_key = str(event.get("game_key", "rare_diamond"))
                        config = TIMED_TREASURE_GAMES.get(game_key, TIMED_TREASURE_GAMES["rare_diamond"])
                        if _game_results_disabled_for_room(target_room):
                            continue
                        if self._effective_room_language(target_room) == "en":
                            notice = f"⌛ {config['title_en']} round closed: the player limit ({required}) was not reached. Next game announcement in 10 minutes."
                        else:
                            notice = f"⌛ أُغلقت جولة {config['title_ar']} لعدم اكتمال العدد ({required}). الإعلان التالي بعد 10 دقائق."
                        self.send_room_text(target_room, notice)
                    except Exception as exc:
                        self.log("[TREASURE-CYCLE] timeout notice failed:", target_room, repr(exc))
                next_start = float(getattr(
                    self, "_rare_diamond_next_due",
                    time.time() + TIMED_TREASURE_ANNOUNCEMENT_INTERVAL_SECONDS,
                ))
            with self._rare_diamond_lock:
                active = isinstance(self._rare_diamond_event, dict) and now < float(self._rare_diamond_event.get("ends_at", 0) or 0)
            if active or now < next_start:
                continue
            game_key = sequence[next_index % len(sequence)]
            try:
                if self._start_rare_diamond_event(game_key):
                    next_index = (next_index + 1) % len(sequence)
                    self._treasure_cycle_index = next_index
                    next_start = float("inf")  # resolver/timeout schedules the ten-minute pause
                else:
                    next_start = time.time() + 10.0
            except Exception as exc:
                self.log("[TREASURE-CYCLE] game start failed:", repr(exc))
                next_start = time.time() + 10.0

    def _start_rare_diamond_event(self, game=None):
        """Open one timed round and localize an engaging announcement per room."""
        game_key = str(game or "rare_diamond")
        config = TIMED_TREASURE_GAMES.get(game_key)
        if not config:
            game_key = "rare_diamond"
            config = TIMED_TREASURE_GAMES[game_key]
        rooms = [str(r).strip() for r in getattr(self, "connected_rooms", set()) if str(r).strip()]
        if not rooms:
            try:
                rooms = [str(r).strip() for r in self._active_rooms() if str(r).strip()]
            except Exception:
                rooms = []
        rooms = sorted(set(rooms), key=str.casefold)
        if not rooms:
            return False
        now = time.time()
        with self._rare_diamond_lock:
            next_due = float(getattr(self, "_rare_diamond_next_due", 0) or 0)
            if now < next_due:
                self.log("[TREASURE-CYCLE] start suppressed until cooldown expires", game_key, next_due)
                return False
            current = self._rare_diamond_event
            if isinstance(current, dict) and now < float(current.get("ends_at", 0) or 0):
                return False
            self._rare_diamond_round_seq += 1
            self._rare_diamond_event = {
                "id": f"treasure-{game_key}-{int(now)}-{self._rare_diamond_round_seq}",
                "game_key": game_key,
                "game_title": config["title_ar"],
                "game_title_ar": config["title_ar"],
                "game_title_en": config["title_en"],
                "room": rooms[0], "rooms": rooms, "players": {},
                "ends_at": now + 180.0, "required_players": int(config["players"]),
            }
            if game_key == "imperial_sickness":
                with self._doctor_ban_lock:
                    self._rare_diamond_event["doctor_banned_once"] = dict(self._doctor_ban_once)
                    self._doctor_ban_once.clear()
        prize = f"{config['prize']:,}"
        for target_room in rooms:
            try:
                if _game_results_disabled_for_room(target_room):
                    continue
                if self._effective_room_language(target_room) == "en":
                    announcement = (
                        f"🌟 THE ADVENTURE IS ON! 🌟\n"
                        f"{config['title_en']}\n{config['story_en']}\n"
                        f"🎮 Join by typing: {config['join_en']}\n"
                        f"👥 Gather {config['players']} brave players to unlock the grand finale.\n"
                        f"🏆 A prize of {prize} points is waiting!\n"
                        "⏳ You have 3 minutes — will you claim the win?"
                    )
                else:
                    announcement = (
                        f"🌟 المغامرة بدأت! 🌟\n"
                        f"{config['title_ar']}\n{config['story_ar']}\n"
                        f"🎮 للانضمام إلى الجولة، اكتب في الغرفة: {config['join_ar']}\n"
                        f"👥 اجمعوا {config['players']} مغامرين لاكتمال التحدي.\n"
                        f"🏆 جائزة كبرى بانتظار الفائز: {prize} نقطة!\n"
                        "⏳ أمامكم 3 دقائق — من سيخطف الفوز؟"
                    )
                self.send_room_text(target_room, announcement)
            except Exception as exc:
                self.log("[TREASURE-CYCLE] room announcement failed:", target_room, repr(exc))
        self.log("[TREASURE-CYCLE] round opened", game_key, "in rooms:", len(rooms))
        return True

    def _join_rare_diamond_event(self, room, username, requested_game=None):
        """Join the current scheduled game; each player is counted once across rooms."""
        name = str(username or "").strip().lstrip("@")
        key = _norm_user(name)
        if not key:
            return False
        language_en = self._effective_room_language(room) == "en"
        if requested_game == "imperial_sickness":
            with self._rare_diamond_lock:
                event = self._rare_diamond_event
                banned_once = event.get("doctor_banned_once", {}) if isinstance(event, dict) else {}
                ban_record = banned_once.get(key) if isinstance(banned_once, dict) else None
            if ban_record:
                if language_en:
                    self.send_room_text(room, f"🩺 @{name}, your medical license is suspended for this doctor round only. Review your notes and try the next one!")
                else:
                    self.send_room_text(room, f"🩺 @{name}، تم إيقافك عن مزاولة الطب في هذه الجولة فقط. راجع كتبك وحاول في الجولة القادمة!")
                return True
        with self._rare_diamond_lock:
            event = self._rare_diamond_event
            if not isinstance(event, dict) or time.time() >= float(event.get("ends_at", 0) or 0):
                return False
            event_rooms = event.get("rooms") or [event.get("room", "")]
            if not any(_norm_room(room) == _norm_room(r) for r in event_rooms):
                return False
            active_key = str(event.get("game_key", "rare_diamond"))
            if requested_game and active_key != requested_game:
                active_title = event.get("game_title_en" if language_en else "game_title_ar", "game")
                active_command = TIMED_TREASURE_GAMES.get(active_key, TIMED_TREASURE_GAMES["rare_diamond"])["join_en" if language_en else "join_ar"]
                mismatch = True
                players = event.setdefault("players", {})
                count = len(players)
                required_players = int(event.get("required_players", 5))
                ready = False
                already_joined = False
                round_id = event.get("id", "")
                joined_rooms = []
            else:
                mismatch = False
                players = event.setdefault("players", {})
                required_players = int(event.get("required_players", 5))
                if key in players:
                    count = len(players)
                    already_joined = True
                    ready = False
                elif len(players) >= required_players:
                    count = len(players)
                    already_joined = False
                    ready = False
                else:
                    players[key] = {"name": name, "room": str(room)}
                    count = len(players)
                    already_joined = False
                    ready = count >= required_players
                round_id = event["id"]
                game_title = event.get("game_title_en" if language_en else "game_title_ar", "game")
                joined_rooms = sorted({
                    str(player.get("room", "")).strip()
                    for player in players.values()
                    if isinstance(player, dict) and str(player.get("room", "")).strip()
                }, key=str.casefold)
        if mismatch:
            if language_en:
                self.send_room_text(room, f"🎮 That game is not open yet. Current round: {active_title}.\n📌 Join command: {active_command}")
            else:
                self.send_room_text(room, f"🎮 هذه اللعبة لم تُفتح بعد. الجولة المفتوحة الآن: {active_title}.\n📌 أمر المشاركة الحالي: {active_command}")
            return True
        if already_joined:
            if language_en:
                self.send_room_text(room, f"🎮 @{name}, you have already joined ({count}/{required_players}).")
            else:
                self.send_room_text(room, f"🎮 @{name} أنت مشارك بالفعل ({count}/{required_players}).")
            return True
        if count >= required_players and not ready:
            self.send_room_text(room, "🎮 All participant slots are full; wait for the next round." if language_en else f"🎮 اكتمل عدد المشاركين ({required_players})؛ انتظر الجولة التالية.")
            return True
        if count == required_players and ready:
            if language_en:
                self.send_room_text(room, f"🎮 @{name} completed the roster ({count}/{required_players})!")
            else:
                self.send_room_text(room, f"🎮 @{name} أكمل العدد ({count}/{required_players})!")
            for target_room in joined_rooms:
                try:
                    if self._effective_room_language(target_room) == "en":
                        ready_notice = f"🔥 All {required_players} players have joined! {TIMED_TREASURE_GAMES[active_key]['title_en']} is starting..."
                    else:
                        ready_notice = f"🔥 اكتمل المشاركون ({required_players})! بدأت جولة {TIMED_TREASURE_GAMES[active_key]['title_ar']}..."
                    self.send_room_text(target_room, ready_notice)
                except Exception as exc:
                    self.log("[TREASURE-CYCLE] ready notice failed:", target_room, repr(exc))
            threading.Thread(target=self._resolve_rare_diamond_event, args=(round_id,), name="treasure-game-resolve", daemon=True).start()
        else:
            if language_en:
                self.send_room_text(room, f"🎮 @{name} joined {TIMED_TREASURE_GAMES[active_key]['title_en']} ({count}/{required_players}).")
            else:
                self.send_room_text(room, f"🎮 @{name} انضم إلى {TIMED_TREASURE_GAMES[active_key]['title_ar']} ({count}/{required_players}).")
        return True

    def _resolve_rare_diamond_event(self, round_id):
        """Resolve a completed round, then start the ten-minute inter-game pause."""
        try:
            return self._resolve_rare_diamond_event_impl(round_id)
        finally:
            lock = getattr(self, "_rare_diamond_lock", None)
            if lock is not None:
                with lock:
                    if (getattr(self, "_rare_diamond_event", None) is None
                            and float(getattr(self, "_rare_diamond_next_due", 0) or 0) == float("inf")):
                        self._rare_diamond_next_due = time.time() + TIMED_TREASURE_ANNOUNCEMENT_INTERVAL_SECONDS

    def _resolve_rare_diamond_event_impl(self, round_id):
        """Resolve one round. Results go only to rooms where its players joined."""
        time.sleep(3.0)
        with self._rare_diamond_lock:
            event = self._rare_diamond_event
            if not isinstance(event, dict) or event.get("id") != round_id:
                return
            player_records = list(event.get("players", {}).values())
            game_key = event.get("game_key", "rare_diamond")
            self._rare_diamond_event = None
            # Keep the cycle worker waiting until all result messages/cards are
            # processed; the wrapper sets the real deadline in its finally block.
            self._rare_diamond_next_due = float("inf")
        config = TIMED_TREASURE_GAMES.get(game_key, TIMED_TREASURE_GAMES["rare_diamond"])
        required_players = int(config["players"])
        if len(player_records) != required_players:
            return
        players = [p.get("name", "") if isinstance(p, dict) else str(p) for p in player_records]
        player_rooms = {
            _norm_user(p.get("name", "")): str(p.get("room", "")).strip()
            for p in player_records if isinstance(p, dict) and p.get("name")
        }
        rooms = sorted({
            str(p.get("room", "")).strip()
            for p in player_records if isinstance(p, dict) and str(p.get("room", "")).strip()
        }, key=str.casefold)
        if not rooms:
            return
        winner = secrets.choice(players)
        winner_key = _norm_user(winner)
        losers = [player for player in players if _norm_user(player) != winner_key]
        prize_amount = int(config["prize"])
        prize_label_ar = f"{prize_amount:,} نقطة"
        prize_label_en = f"{prize_amount:,} points"
        try:
            winner_credit = _apply_points_delta_once(
                winner, prize_amount, f"{round_id}:winner:{winner_key}", f"{game_key} prize",
                repay_loan=True,
            )
            self._notify_loan_repayment(winner, winner_credit)
        except Exception as exc:
            self.log("[TREASURE-CYCLE] winner credit failed:", winner, repr(exc))
            winner_credit = {"ok": False}

        # Losers skip exactly the next doctor round; this is temporary, not a permanent ban.
        if game_key == "imperial_sickness":
            with self._doctor_ban_lock:
                for player in losers:
                    key = _norm_user(player)
                    if key:
                        self._doctor_ban_once[key] = {"name": player, "round_id": round_id}
            for player in losers:
                try:
                    _apply_points_delta_once(
                        player, -10_000, f"{round_id}:loss:{_norm_user(player)}", f"{game_key} participation"
                    )
                except Exception as exc:
                    self.log("[TREASURE-CYCLE] doctor loss debit failed:", player, repr(exc))
            for target_room in rooms:
                if _game_results_disabled_for_room(target_room):
                    continue
                language_en = self._effective_room_language(target_room) == "en"
                if language_en:
                    result_text = (
                        f"✅ @{winner} saved the Emperor and won {prize_label_en}! 👑🩺"
                        if isinstance(winner_credit, dict) and winner_credit.get("ok")
                        else f"🏥 @{winner} saved the Emperor, but the {prize_label_en} prize could not be credited."
                    )
                else:
                    result_text = (
                        f"✅ نجح الطبيب @{winner} في إنقاذ الإمبراطور وفاز بـ{prize_label_ar}! 👑🩺"
                        if isinstance(winner_credit, dict) and winner_credit.get("ok")
                        else f"🏥 أنقذ @{winner} الإمبراطور، لكن تعذّر اعتماد جائزة {prize_label_ar}."
                    )
                blocked = ", ".join(f"@{player}" for player in losers) if language_en else "، ".join(f"@{player}" for player in losers)
                if language_en:
                    result_text += f"\n🚫 One doctor round only: sit out {blocked}; after that, you can join doctor again."
                else:
                    result_text += f"\n🚫 الخاسرون لجولة طبيب واحدة فقط: {blocked}\nبعد الجولة التالية تستطيعون المشاركة مجدداً."
                try:
                    self.send_room_text(target_room, result_text)
                    self._send_treasure_game_result_card(target_room, game_key, winner)
                except Exception as exc:
                    self.log("[TREASURE-CYCLE] doctor result send failed:", target_room, repr(exc))
            return

        # Apply each non-winner's participation deduction once, then reuse the same
        # outcome in every participant room's localized result.
        debit_results = {}
        for player in players:
            if _norm_user(player) == winner_key:
                continue
            try:
                debit_results[_norm_user(player)] = _apply_points_delta_once(
                    player, -10_000, f"{round_id}:loss:{_norm_user(player)}", f"{game_key} participation"
                )
            except Exception as exc:
                self.log("[TREASURE-CYCLE] loss debit failed:", player, repr(exc))
                debit_results[_norm_user(player)] = {"ok": False}

        for target_room in rooms:
            if _game_results_disabled_for_room(target_room):
                continue
            language_en = self._effective_room_language(target_room) == "en"
            if language_en:
                lines = [
                    f"🏆 @{winner} won {config['title_en']}!",
                    f"💰 Prize: {prize_label_en}." if isinstance(winner_credit, dict) and winner_credit.get("ok")
                    else f"⚠️ The {prize_label_en} prize could not be credited; the bot log should be checked.",
                ]
            else:
                lines = [
                    f"🏆 @{winner} فاز في لعبة {config['title_ar']}!",
                    f"💰 الجائزة: {prize_label_ar}." if isinstance(winner_credit, dict) and winner_credit.get("ok")
                    else f"⚠️ تعذّر اعتماد جائزة {prize_label_ar} للفائز؛ راجع سجل البوت.",
                ]
            for player in players:
                if _norm_user(player) == winner_key:
                    continue
                debit = debit_results.get(_norm_user(player), {"ok": False})
                if language_en:
                    if isinstance(debit, dict) and debit.get("ok"):
                        lines.append(f"@{player}: -10,000 points")
                    else:
                        lines.append(f"@{player}: the 10,000-point deduction was not applied (insufficient balance or update error).")
                else:
                    if isinstance(debit, dict) and debit.get("ok"):
                        lines.append(f"@{player}: -10,000 نقطة")
                    else:
                        lines.append(f"@{player}: لم يُخصم 10,000 نقطة لعدم كفاية الرصيد أو تعذّر التحديث.")
            try:
                self.send_room_text(target_room, "\n".join(lines))
                self._send_treasure_game_result_card(target_room, game_key, winner)
            except Exception as exc:
                self.log("[TREASURE-CYCLE] result send failed:", target_room, repr(exc))
