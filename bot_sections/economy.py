"""Points, loans, wallet, shop, mining and stock-market bot methods."""

class EconomySection:
    def _recover_stock_market_investments_from_points_history(self):
        """Rebuild missing pending stock rows from their durable point-debit journal."""
        try:
            _points_data()  # Loads the shared transfer journal on a cold start.
            transactions = [row for row in (_POINTS_TRANSFER_CACHE or []) if isinstance(row, dict)]
        except Exception as exc:
            self.log("[STOCK] point-journal recovery read failed:", repr(exc))
            return 0
        with self._stock_market_lock:
            known_ids = {
                str(row.get("id") or "")
                for row in getattr(self, "stock_investments", []) + getattr(self, "stock_market_history", [])
                if isinstance(row, dict)
            }
            paid_ids = {
                str(row.get("id") or "").partition("stock-market-payout:")[2]
                for row in transactions
                if str(row.get("id") or "").startswith("stock-market-payout:")
            }
            recovered = []
            for transaction in transactions:
                investment = transaction.get("stock_market_investment")
                if not isinstance(investment, dict):
                    continue
                investment_id = str(investment.get("id") or "").strip()
                if (not investment_id or investment_id in known_ids or investment_id in paid_ids
                        or not investment.get("username")):
                    continue
                row = copy.deepcopy(investment)
                row.setdefault("status", "pending_debit")
                recovered.append(row)
                known_ids.add(investment_id)
            if not recovered:
                return 0
            self.stock_investments.extend(recovered)
            self._save_stock_investments()
        self.log("[STOCK] recovered pending investments from points journal:", len(recovered))
        return len(recovered)

    def _record_stock_market_history(self, result):
        """Persist one completed investment exactly once for the user's بورصتي log."""
        if not isinstance(result, dict) or not result.get("id") or not result.get("username"):
            return False
        with self._stock_market_lock:
            history = [dict(row) for row in getattr(self, "stock_market_history", []) if isinstance(row, dict)]
            if any(str(row.get("id")) == str(result.get("id")) for row in history):
                self.stock_market_history = history
                return True
            history.append(dict(result))
            try:
                _save_local_json(STOCK_MARKET_HISTORY_FILE, {"version": 1, "history": history})
            except Exception as exc:
                self.log("[STOCK] history save failed; settlement will retry:", repr(exc))
                return False
            self.stock_market_history = history
            return True

    def _stock_market_announcement(self, language):
        if language == "en":
            return "📈 Stock market is open! Type stock, choose an asset and amount. You can invest again every 2 minutes. Result in 1 minute: profit or loss."
        return "📈 بدأت البورصة! أرسل بورصة، واختر الأصل والمبلغ. يمكنك الاستثمار مجدداً كل دقيقتين. النتيجة بعد دقيقة: ربح أو خسارة."

    def _broadcast_stock_market_round(self, rooms):
        # A round belongs to one room; never announce it across all rooms.
        for target_room in list(rooms)[:1]:
            if _game_results_disabled_for_room(target_room):
                continue
            language = self._configured_room_language(target_room)
            try:
                self.send_room_text(target_room, self._stock_market_announcement(language))
            except Exception as exc:
                self.log("[STOCK] round announcement failed:", target_room, repr(exc))

    def _queue_stock_market_round_broadcast(self, rooms):
        if not STOCK_MARKET_ROOM_ANNOUNCEMENTS_ENABLED:
            self.log("[STOCK] room-wide announcements disabled")
            return
        worker = threading.Thread(
            target=self._broadcast_stock_market_round,
            args=(list(rooms),),
            daemon=True,
            name="stock-market-announcement",
        )
        worker.start()

    def _stock_market_message(self, language):
        if language == "en":
            return (
                "📈 Stock market\n1️⃣ 🥇 Gold  2️⃣ 🥈 Silver  3️⃣ 🛢️ Oil  4️⃣ 🏞️ Land\n"
                "Send a number then the amount, or type `gold 100000`.\n"
                "⏱️ Result in 1 minute: gains or losses.\n⏳ You can invest again every 2 minutes."
            )
        return (
            "📈 البورصة\n1️⃣ 🥇 ذهب  2️⃣ 🥈 فضة  3️⃣ 🛢️ نفط  4️⃣ 🏞️ أرض\n"
            "أرسل رقم الخيار ثم المبلغ، أو اكتب `ذهب 100000`.\n"
            "⏱️ النتيجة بعد دقيقة؛ تقلبات السوق قد تحقق ربحاً أو خسارة.\n⏳ يمكنك الاستثمار مجدداً كل دقيقتين."
        )

    def _stock_market_is_open(self, now=None):
        now = time.time() if now is None else float(now)
        event = getattr(self, "stock_market_round", None)
        return bool(event and now < float(event.get("ends_at", 0) or 0))

    def _start_stock_market_round(self, now=None, *, force=False, first_room=""):
        now = time.time() if now is None else float(now)
        lock = getattr(self, "_stock_market_lock", None)
        if lock is None:
            self._stock_market_lock = lock = threading.RLock()
        with lock:
            current = getattr(self, "stock_market_round", None)
            if not force and current and now < float(current.get("ends_at", 0) or 0):
                return current
            self._stock_market_round_seq = int(getattr(self, "_stock_market_round_seq", 0)) + 1
            event = {
                "id": self._stock_market_round_seq,
                "started_at": now,
                "ends_at": now + STOCK_MARKET_INTERVAL_SECONDS,
            }
            self.stock_market_round = event
            self._stock_market_seen = set()
            self._stock_market_next_announcement = now + STOCK_MARKET_INTERVAL_SECONDS
        # Manual rounds announce only in their originating room. Scheduled
        # rounds pick one active room rather than broadcasting to every room.
        try:
            active_rooms = {str(room).strip() for room in self._active_rooms() if str(room).strip()}
        except Exception:
            active_rooms = {str(room).strip() for room in getattr(self, "connected_rooms", set()) if str(room).strip()}
        if first_room:
            ordered_rooms = [str(first_room).strip()]
        else:
            ordered_rooms = sorted(
                (room for room in active_rooms if not _game_results_disabled_for_room(room)),
                key=str.casefold,
            )[:1]
        self._queue_stock_market_round_broadcast(ordered_rooms)
        return event

    def _notify_loan_repayment(self, username, result):
        if not isinstance(result, dict) or int(result.get("loan_repayment", 0) or 0) <= 0:
            return False
        try:
            self.send_private_text(
                username,
                f"💸 تم اقتطاع {_fmt_points(result.get('loan_repayment', 0))} نقطة من مكسبك لسداد سلفة 100 مليار. "
                f"المتبقي من السلفة: {_fmt_points(result.get('loan_debt', 0))} نقطة.",
            )
            return True
        except Exception as exc:
            self.log("[LOAN] repayment notice failed:", repr(exc))
            return False

    def _wallet_menu_key(self, room, username):
        return (_norm_room(room), _norm_user(username))

    def _wallet_show_catalog(self, room, username, page=1):
        page = max(1, min(2, int(page or 1)))
        state = getattr(self, "wallet_pending_pages", {})
        if not isinstance(state, dict):
            state = {}
        state[self._wallet_menu_key(room, username)] = {"mode": "catalog", "page": page, "created": time.time()}
        self.wallet_pending_pages = state
        start = (page - 1) * WALLET_PAGE_SIZE
        assets = list(WALLET_ASSETS.items())[start:start + WALLET_PAGE_SIZE]
        lines = [f"🧳 محفظة الاستثمارات — القائمة {page}/2", "اختر سلعة ثم أرسل: شراء اسم السلعة المبلغ"]
        lines.extend(f"{asset_id}. {asset['icon']} {asset['ar']} ({asset['en']})" for asset_id, asset in assets)
        lines.append("مثال: شراء ذهب 30b — كل قطعة تُباع تلقائيًا بعد ساعة بربح 5% أو خسارة 5%.")
        lines.append("اكتب ns لعرض القائمة التالية. يمكنك شراء عدة سلع.")
        self.send_room_text(room, "\n".join(lines))
        return True

    def _wallet_show_holdings(self, room, username, page=1):
        accounts = _points_data()
        resolved = _resolve_points_account(accounts, username)
        positions = []
        if resolved:
            item = resolved[1]
            positions = [dict(row) for row in item.get("wallet_positions", [])
                         if isinstance(row, dict)] if isinstance(item.get("wallet_positions"), list) else []
        pages = max(1, (len(positions) + WALLET_PAGE_SIZE - 1) // WALLET_PAGE_SIZE)
        page = max(1, min(pages, int(page or 1)))
        state = getattr(self, "wallet_pending_pages", {})
        if not isinstance(state, dict):
            state = {}
        state[self._wallet_menu_key(room, username)] = {"mode": "holdings", "page": page, "pages": pages, "created": time.time()}
        self.wallet_pending_pages = state
        start = (page - 1) * WALLET_PAGE_SIZE
        selected = positions[start:start + WALLET_PAGE_SIZE]
        if not positions:
            self.send_room_text(room, "🧳 محفظتك فارغة حاليًا. أرسل محفظة لعرض السلع أو شراء سلعة.")
            return True
        lines = [f"🧳 القطع النشطة في محفظتك — الصفحة {page}/{pages}"]
        for row in selected:
            asset = WALLET_ASSETS.get(int(row.get("asset_id", 0) or 0), {})
            remaining = max(0, int(float(row.get("due_at", 0) or 0) - time.time()))
            lines.append(
                f"{asset.get('icon','📦')} {asset.get('ar','سلعة')} | قيمة الشراء: {_fmt_points(row.get('amount', 0))} نقطة | "
                f"البيع خلال {remaining // 60} دقيقة"
            )
        if pages > page:
            lines.append("اكتب ns لعرض المزيد.")
        self.send_room_text(room, "\n".join(lines))
        return True

    def _wallet_handle_next_page(self, room, username, text):
        if str(text or "").strip().casefold() not in {"ns", "التالي", "القائمة التالية", "next"}:
            return False
        state = getattr(self, "wallet_pending_pages", {})
        key = self._wallet_menu_key(room, username)
        entry = state.get(key) if isinstance(state, dict) else None
        if not isinstance(entry, dict):
            return False
        if time.time() - float(entry.get("created", 0) or 0) > 600:
            state.pop(key, None)
            return False
        page = int(entry.get("page", 1) or 1)
        if entry.get("mode") == "catalog":
            if page >= 2:
                self.send_room_text(room, "📋 هذه آخر قائمة. أرسل محفظة للعودة للقائمة الأولى.")
                return True
            return self._wallet_show_catalog(room, username, page + 1)
        if entry.get("mode") == "holdings":
            if page >= int(entry.get("pages", 1) or 1):
                self.send_room_text(room, "📋 لا توجد صفحات أخرى في المحفظة.")
                return True
            return self._wallet_show_holdings(room, username, page + 1)
        return False

    def _wallet_purchase_command(self, room, username, parsed_order):
        asset_id, amount = parsed_order
        result = _create_wallet_position(username, asset_id, amount, room=room)
        if result.get("ok"):
            asset = WALLET_ASSETS[asset_id]
            position = result.get("position", {})
            self.send_room_text(
                room,
                f"✅ أُضيفت {asset['icon']} {asset['ar']} إلى محفظتك بقيمة {_fmt_points(amount)} نقطة. "
                f"سيتم بيعها تلقائيًا بعد ساعة بربح 5% أو خسارة 5%. "
                f"رصيدك المتبقي: {_fmt_points(result.get('balance', 0))}.",
            )
            self.log("[WALLET] position created", username, position.get("id"), asset_id, amount)
            return True
        messages = {
            "target_not_found": "❌ لا يوجد سجل نقاط لهذا الحساب؛ العب أو اكسب نقاطًا أولًا.",
            "insufficient": f"❌ رصيدك لا يكفي. رصيدك الحالي: {_fmt_points(result.get('balance', 0))} نقطة.",
            "amount_out_of_range": f"❌ قيمة القطعة يجب أن تكون بين {_fmt_points(WALLET_MIN_INVESTMENT)} و{_fmt_points(WALLET_MAX_INVESTMENT)} نقطة.",
            "too_many_positions": f"❌ وصلت للحد الأقصى ({WALLET_MAX_ACTIVE_POSITIONS}) من القطع النشطة. انتظر بيع بعضها أولًا.",
        }
        self.send_room_text(room, messages.get(result.get("error"), "❌ تعذر شراء القطعة الآن؛ لم يُخصم شيء."))
        return True

    def _process_wallet_investments(self, now=None):
        now = time.time() if now is None else float(now)
        for username, position_id in _wallet_due_positions(now):
            result = _settle_wallet_position(username, position_id, now=now)
            if not result.get("ok") or not result.get("settled"):
                continue
            asset = WALLET_ASSETS.get(int(result.get("asset_id", 0) or 0), {})
            outcome = result.get("outcome")
            direction = "ربح 5%" if outcome == "profit" else "خسارة 5%"
            sign = "+" if int(result.get("net_change", 0) or 0) >= 0 else "-"
            message = (
                f"📦 تم بيع {asset.get('icon','')} {asset.get('ar','القطعة')} من محفظتك تلقائيًا بعد ساعة.\n"
                f"النتيجة: {direction}.\n"
                f"قيمة الشراء: {_fmt_points(result.get('principal', 0))} نقطة\n"
                f"العائد قبل سداد السلفة: {_fmt_points(result.get('payout', 0))} نقطة "
                f"({sign}{_fmt_points(abs(int(result.get('net_change', 0) or 0)))}).\n"
            )
            if int(result.get("loan_repayment", 0) or 0) > 0:
                message += (
                    f"💸 سداد السلفة من الربح: {_fmt_points(result['loan_repayment'])} نقطة.\n"
                    f"المتبقي من السلفة: {_fmt_points(result.get('loan_debt', 0))} نقطة.\n"
                )
            message += f"💳 رصيدك الحالي: {_fmt_points(result.get('balance', 0))} نقطة."
            try:
                self.send_private_text(username, message)
            except Exception as exc:
                self.log("[WALLET] private sale notice failed:", username, repr(exc))
            self.log("[WALLET] position settled", username, position_id, outcome, result.get("payout"))

    def _wallet_settlement_worker(self):
        """Settle matured wallet assets independently of stock-market settings."""
        while not self.stop_event.is_set():
            try:
                self._process_wallet_investments()
            except Exception as exc:
                self.log("[WALLET] settlement worker iteration failed; will retry:", repr(exc))
            self.stop_event.wait(2.0)

    def _stock_market_worker(self):
        while not self.stop_event.is_set():
            try:
                now = time.time()
                next_announcement = float(getattr(self, "_stock_market_next_announcement", 0) or 0)
                if STOCK_MARKET_ROOM_ANNOUNCEMENTS_ENABLED and now >= next_announcement and self._active_rooms():
                    self._start_stock_market_round(now, force=True)
                self._cleanup_expired_wagers()
                self._process_stock_market_investments(now)
            except Exception as exc:
                self.log("[STOCK] worker iteration failed; will retry:", repr(exc))
            self.stop_event.wait(2.0)

    def _stock_market_invest(self, room, sender, asset, amount, *, expected_round_id=None):
        try:
            asset = int(asset)
            amount = int(amount)
        except (TypeError, ValueError):
            asset, amount = 0, 0
        language = self._configured_room_language(room)
        if asset not in STOCK_MARKET_ASSETS or amount < 10:
            self.send_room_text(room, "❌ الحد الأدنى للاستثمار 10 نقاط." if language != "en" else "❌ The minimum investment is 10 points.")
            return True
        if amount > STOCK_MARKET_MAX_INVESTMENT:
            text = (
                "❌ أعلى سقف للاستثمار في البورصة هو 30b (30 مليار نقطة)."
                if language != "en" else
                "❌ The maximum stock-market investment is 30b (30 billion points)."
            )
            self.send_room_text(room, text)
            return True
        now = time.time()
        user_key = _norm_user(sender)
        previous_cooldown = None
        previous_cooldown_present = False
        cooldown_remaining = 0.0
        with self._stock_market_lock:
            cooldowns = getattr(self, "_stock_market_user_cooldowns", {})
            if not isinstance(cooldowns, dict):
                cooldowns = {}
            last_investment = float(cooldowns.get(user_key, 0) or 0)
            if (not _is_env_master_for_game_cooldown(sender)) and STOCK_MARKET_PLAYER_COOLDOWN_SECONDS > 0 and last_investment and now - last_investment < STOCK_MARKET_PLAYER_COOLDOWN_SECONDS:
                cooldown_remaining = STOCK_MARKET_PLAYER_COOLDOWN_SECONDS - (now - last_investment)
                closed = False
                round_id = None
                duplicate = False
                investment = None
            else:
                event = self.stock_market_round
                if not event or now >= float(event.get("ends_at", 0) or 0) or (
                    expected_round_id is not None and str(expected_round_id) != str(event.get("id"))
                ):
                    closed = True
                    round_id = None
                else:
                    closed = False
                    round_id = event.get("id")
                if closed:
                    investment = None
                else:
                    previous_cooldown_present = user_key in cooldowns
                    previous_cooldown = cooldowns.get(user_key)
                    cooldowns[user_key] = now
                    self._stock_market_user_cooldowns = cooldowns
                    investment = {
                        "id": uuid.uuid4().hex,
                        "event_id": round_id,
                        "username": str(sender).strip().lstrip("@"),
                        "room": str(room or ""),
                        "asset": asset,
                        "amount": amount,
                        "started_at": now,
                        "due_at": now + STOCK_MARKET_SETTLEMENT_SECONDS,
                        "status": "pending_debit",
                    }
                    self.stock_investments.append(investment)
                    # A player may invest repeatedly during the same market round;
                    # the two-minute cooldown is the only per-player restriction.
        if cooldown_remaining > 0:
            seconds = max(1, int(cooldown_remaining + 0.999))
            text = (
                f"⏳ فاصل البورصة دقيقتان؛ انتظر {seconds} ثانية قبل الاستثمار التالي."
                if language != "en" else
                f"⏳ There is a 2-minute cooldown between investments; try again in {seconds} seconds."
            )
            self.send_room_text(room, text)
            return True
        if closed:
            self.send_room_text(room, "⌛ انتهت جولة البورصة؛ انتظر الإعلان التالي أو اكتب بورصة لبدء جولة." if language != "en" else "⌛ This market round has closed. Wait for the next announcement or type stock to start a round.")
            return True
        if investment is None:
            self.send_room_text(room, "⚠️ استثمرت مرة واحدة في هذه الجولة؛ انتظر النتيجة أو الجولة التالية." if language != "en" else "⚠️ You already invested in this round. Wait for the result or next round.")
            return True
        try:
            self._save_stock_investments()
        except Exception as exc:
            with self._stock_market_lock:
                self.stock_investments = [row for row in self.stock_investments if row.get("id") != investment["id"]]
                self._stock_market_seen.discard((str(round_id), _norm_user(sender)))
                cooldowns = getattr(self, "_stock_market_user_cooldowns", {})
                if isinstance(cooldowns, dict) and cooldowns.get(user_key) == now:
                    if previous_cooldown_present:
                        cooldowns[user_key] = previous_cooldown
                    else:
                        cooldowns.pop(user_key, None)
            self.log("[STOCK] could not save pending investment:", repr(exc))
            self.send_room_text(room, "❌ تعذر حفظ الاستثمار، لم يُخصم أي رصيد." if language != "en" else "❌ The investment could not be saved; no points were deducted.")
            return True
        debit = _apply_points_delta_once(
            sender, -amount, f"stock-market-debit:{investment['id']}",
            f"stock market investment in {STOCK_MARKET_ASSETS[asset]['en']}",
        )
        if not debit.get("ok"):
            with self._stock_market_lock:
                self.stock_investments = [row for row in self.stock_investments if row.get("id") != investment["id"]]
                self._stock_market_seen.discard((str(round_id), _norm_user(sender)))
                cooldowns = getattr(self, "_stock_market_user_cooldowns", {})
                if isinstance(cooldowns, dict) and cooldowns.get(user_key) == now:
                    if previous_cooldown_present:
                        cooldowns[user_key] = previous_cooldown
                    else:
                        cooldowns.pop(user_key, None)
            try:
                self._save_stock_investments()
            except Exception as exc:
                self.log("[STOCK] failed to remove rejected investment:", repr(exc))
            if debit.get("error") == "insufficient":
                text = f"❌ رصيدك لا يكفي لاستثمار {_fmt_points(amount)} نقطة." if language != "en" else f"❌ You do not have enough points to invest {_fmt_points(amount)}."
            else:
                text = "❌ تعذر خصم مبلغ الاستثمار بأمان؛ لم تُعتمد العملية." if language != "en" else "❌ The investment debit could not be committed safely."
            self.send_room_text(room, text)
            return True
        with self._stock_market_lock:
            for row in self.stock_investments:
                if row.get("id") == investment["id"]:
                    row["status"] = "active"
                    break
        try:
            self._save_stock_investments()
        except Exception as exc:
            # The durable pending_debit record is recoverable because the debit
            # itself has a stable transaction id and cannot be applied twice.
            self.log("[STOCK] active state save deferred; recovery will reconcile:", repr(exc))
        data = STOCK_MARKET_ASSETS[asset]
        if language == "en":
            text = f"✅ Investment accepted: {data['icon']} {data['en']}\n💰 Amount: {_fmt_points(amount)} points\n⏱️ Result in 1 minute: profit or loss.\n⏳ You can invest again in 2 minutes."
        else:
            text = f"✅ تم قبول الاستثمار: {data['icon']} {data['ar']}\n💰 المبلغ: {_fmt_points(amount)} نقطة\n⏱️ النتيجة بعد دقيقة: ربح أو خسارة.\n⏳ يمكنك الاستثمار مجدداً بعد دقيقتين."
        self.send_room_text(room, text)
        return True

    def _broadcast_stock_market_result(self, username, source_room, asset, amount, outcome, change_percent, change):
        """Announce a settled investment only in its source room."""
        if not STOCK_MARKET_ROOM_ANNOUNCEMENTS_ENABLED:
            return
        target_room = str(source_room or "").strip()
        if not target_room or _game_results_disabled_for_room(target_room):
            return
        data = STOCK_MARKET_ASSETS.get(int(asset), {})
        is_profit = str(outcome) == "profit"
        sign = "+" if is_profit else "-"
        display_user = _transfer_log_safe_field(username, max_bytes=80)
        english = self._configured_room_language(target_room) == "en"
        if english:
            label = "PROFIT" if is_profit else "LOSS"
            text = (
                f"📈 Stock-market result — {label}\n"
                f"👤 Investor: @{display_user}\n"
                f"{data.get('icon', '📈')} {data.get('en', 'Market')} | Invested: {_fmt_points(amount)} points\n"
                f"📊 {label}: ({sign}{_fmt_points(change)})"
            )
        else:
            label = "ربح" if is_profit else "خسارة"
            text = (
                f"📈 نتيجة البورصة — {label}\n"
                f"👤 المستثمر: @{display_user}\n"
                f"{data.get('icon', '📈')} {data.get('ar', 'البورصة')} | مبلغ الاستثمار: {_fmt_points(amount)} نقطة\n"
                f"📊 {label}: ({sign}{_fmt_points(change)})"
            )
        try:
            self.send_room_text(target_room, text)
        except Exception as exc:
            self.log("[STOCK] result broadcast failed:", target_room, repr(exc))

    def _process_stock_market_investments(self, now=None):
        now = time.time() if now is None else float(now)
        with self._stock_market_lock:
            pending = [dict(row) for row in self.stock_investments if isinstance(row, dict)]
        settled = 0
        for row in pending:
            investment_id = str(row.get("id") or "")
            username = str(row.get("username") or "")
            try:
                amount = int(row.get("amount", 0) or 0)
                asset = int(row.get("asset", 0) or 0)
                due_at = float(row.get("due_at", 0) or 0)
            except (TypeError, ValueError):
                continue
            if not investment_id or not username or amount <= 0 or asset not in STOCK_MARKET_ASSETS:
                continue
            status = str(row.get("status") or "pending_debit")
            if status == "pending_debit":
                debit = _apply_points_delta_once(
                    username, -amount, f"stock-market-debit:{investment_id}",
                    f"stock market investment in {STOCK_MARKET_ASSETS[asset]['en']}",
                )
                if not debit.get("ok"):
                    if debit.get("error") == "persistence_failed":
                        continue
                    with self._stock_market_lock:
                        self.stock_investments = [item for item in self.stock_investments if item.get("id") != investment_id]
                    try:
                        self._save_stock_investments()
                    except Exception:
                        pass
                    continue
                with self._stock_market_lock:
                    for item in self.stock_investments:
                        if item.get("id") == investment_id:
                            item["status"] = "active"
                            break
                try:
                    self._save_stock_investments()
                except Exception as exc:
                    self.log("[STOCK] recovery state save failed:", repr(exc))
                status = "active"
            if now < due_at:
                continue
            if status != "paying":
                outcome = "loss" if secrets.randbelow(2) else "profit"
                change_percent = secrets.randbelow(50) + 1
                change = amount * change_percent // 100
                with self._stock_market_lock:
                    for item in self.stock_investments:
                        if item.get("id") == investment_id:
                            item.update(
                                status="paying", outcome=outcome,
                                change_percent=change_percent, profit_percent=change_percent,
                                change=change, profit=change,
                            )
                            row = dict(item)
                            break
            else:
                outcome = str(row.get("outcome") or "profit")
                stored_percent_value = row.get("change_percent", row.get("profit_percent", 10))
                stored_percent = int(10 if stored_percent_value in (None, "") else stored_percent_value)
                change_percent = max(1, min(50, stored_percent))
                if change_percent != stored_percent:
                    change = amount * change_percent // 100
                    with self._stock_market_lock:
                        for item in self.stock_investments:
                            if item.get("id") == investment_id:
                                item.update(
                                    change_percent=change_percent, profit_percent=change_percent,
                                    change=change, profit=change,
                                )
                                row = dict(item)
                                break
                else:
                    change = int(row.get("change", row.get("profit", amount * change_percent // 100)) or 0)
            try:
                self._save_stock_investments()
            except Exception as exc:
                self.log("[STOCK] payout state save failed; retrying:", repr(exc))
                continue
            payout = max(0, amount - change) if outcome == "loss" else amount + change
            if payout == 0:
                # A 100% loss needs no credit transaction. The points ledger
                # rejects zero-value deltas, but the already-debited stake is
                # still fully settled and must not remain stuck in "paying".
                credit = {"ok": True, "balance": _get_points(username), "already_applied": True}
            else:
                credit = _apply_points_delta_once(
                    username, payout, f"stock-market-payout:{investment_id}",
                    f"stock market {outcome} settlement", repay_loan=True,
                    loan_repayment_basis=max(0, payout - amount),
                )
            if not credit.get("ok"):
                self.log("[STOCK] settlement failed; investment kept for retry:", investment_id, credit.get("error"))
                continue
            self._notify_loan_repayment(username, credit)
            net_change = payout - amount
            history_row = dict(row)
            history_row.update({
                "status": "settled", "payout": payout, "net_change": net_change,
                "balance_after": int(credit.get("balance", 0) or 0), "settled_at": now,
            })
            if not self._record_stock_market_history(history_row):
                # Keep the paying row so the idempotent credit and history write
                # can be retried after a transient storage error.
                continue
            _record_game(username, "stock_market", net_change, amount)
            room = str(row.get("room") or "")
            language = self._configured_room_language(room)
            data = STOCK_MARKET_ASSETS[asset]
            display_user = _transfer_log_safe_field(username, max_bytes=80)
            result_label = "Loss" if outcome == "loss" else "Profit"
            result_sign = "-" if outcome == "loss" else "+"
            if language == "en":
                result_text = (
                    f"📈 Your stock-market result — {result_label}\n👤 Investor: @{display_user}\n"
                    f"{data['icon']} {data['en']} | Invested: {_fmt_points(amount)} points\n"
                    f"📊 {result_label}: ({result_sign}{_fmt_points(change)})\n"
                    f"💳 Balance: {_fmt_points(credit['balance'])}"
                )
            else:
                result_label = "الخسارة" if outcome == "loss" else "الربح"
                result_text = (
                    f"📈 نتيجتك في البورصة — {result_label}\n👤 المستثمر: @{display_user}\n"
                    f"{data['icon']} {data['ar']} | مبلغ الاستثمار: {_fmt_points(amount)} نقطة\n"
                    f"📊 {result_label}: ({result_sign}{_fmt_points(change)})\n"
                    f"💳 الرصيد: {_fmt_points(credit['balance'])}"
                )
            try:
                self._broadcast_stock_market_result(
                    username, room, asset, amount, outcome, change_percent, change
                )
            except Exception as exc:
                self.log("[STOCK] result broadcast failed:", repr(exc))
            try:
                private_text = result_text + (
                    f"\n🏠 Investment room: {room}" if language == "en"
                    else f"\n🏠 غرفة الاستثمار: {room}"
                )
                self.send_private_text(username, private_text)
            except Exception as exc:
                self.log("[STOCK] private result delivery failed:", username, repr(exc))
            with self._stock_market_lock:
                self.stock_investments = [item for item in self.stock_investments if item.get("id") != investment_id]
            try:
                self._save_stock_investments()
            except Exception as exc:
                # If removal could not be saved, the payout transaction id makes
                # the next retry harmless and no points can be paid twice.
                with self._stock_market_lock:
                    if not any(item.get("id") == investment_id for item in self.stock_investments):
                        self.stock_investments.append(dict(row))
                self.log("[STOCK] settled investment cleanup deferred:", repr(exc))
            settled += 1
        return settled

    def _resume_pending_mining_redemption(self, room, sender_name, user_id):
        pending = MINING_STORE.pending_redemption(user_id)
        if not pending:
            return None
        transaction_id = f"mining-redemption:{pending.get('id') or ''}"
        credited = _apply_points_delta_once(
            sender_name, int(pending.get("points", 0) or 0),
            transaction_id, "treasure mining redemption", repay_loan=True,
        )
        if not credited.get("ok"):
            self.send_room_text(
                room,
                "⚠️ يوجد استبدال محفوظ ينتظر اعتماد النقاط. لم أخصم أو أضف شيئًا مرتين؛ أعد المحاولة لاحقًا.",
            )
            return False
        MINING_STORE.complete_redemption(user_id, pending.get("id"))
        self.send_room_text(
            room,
            f"✅ اكتملت تسوية استبدال الكنوز السابق لـ@{sender_name}: "
            f"+{int(pending.get('points', 0) or 0):,} نقطة.",
        )
        return True

    def _execute_mining_command_with_id(self, room, text, sender_name, user_id):
        """Execute immediately with username identity; numeric Talkin ID is a fallback."""
        user_id = str(user_id or "").strip()
        if not user_id:
            # MiningStore uses a stable namespaced key until a native ID is
            # available. This prevents commands from waiting on members_list.
            normalized = _norm_user(sender_name)
            if normalized:
                user_id = "name:" + normalized
        if not user_id:
            return False
        raw = str(text or "").strip()
        low = raw.casefold()
        low = low.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        low = low.replace("ى", "ي")
        store_commands = {"متجر الكنوز", "سوق الكنوز", "treasure shop", "treasure store", "store treasures"}
        inventory_commands = {"sv", "مخزني", "مخزون", "مخزون الكنوز", "inventory", "my inventory", "bag", "sv inventory", "sv bag"}
        top_commands = {"توب الماس", "توب الالماس", "top diamonds", "top gems", "أغنى المنقبين", "اغنى المنقبين"}
        redeem_match = re.fullmatch(r"(?:r|rdem)\s+(.+)", raw, re.I)
        red_code_match = re.fullmatch(r"red(?:\s+(.+))?", raw, re.I)
        is_diamond_test = low in {"فحص الماس", "فحص الالماس"}
        is_rare_mining_test = low in {"فحص التنقيب", "فحص الماسة النادرة", "فحص الالماسة النادرة"}
        is_rare_event_join = low in {"تنقيب", "نقب", "rare diamond", "rare-diamond"}
        is_dig = low in {"الماس", "تنقيب", "نقب", "احفر", "dig", "mine", "mining", "diamond", "diamonds"}
        is_store = low in store_commands
        is_inventory = low in inventory_commands
        is_top = low in top_commands
        if is_rare_event_join:
            try:
                if self._join_rare_diamond_event(room, sender_name):
                    return True
            except Exception as exc:
                self.log("[RARE-DIAMOND] join failed:", repr(exc))
            # The timed rare expedition is separate from ordinary mining.
            # If no event is open, never fall through to the iron/normal dig game.
            self.send_room_text(room, "💎 لا توجد جولة ألماس نادر مفتوحة الآن. انتظر الإعلان القادم بعد 10 دقائق.")
            return True
        if not (is_store or is_inventory or is_top or redeem_match or red_code_match or is_dig or is_diamond_test or is_rare_mining_test):
            return False

        if is_rare_mining_test:
            if not _is_master_name(sender_name):
                self.send_room_text(room, "🔒 أمر فحص التنقيب مخصص للماستر فقط.")
                return True
            if not room:
                self.send_private_text(sender_name, "📌 أرسل أمر فحص التنقيب داخل الغرفة ليظهر اسمك على صورة الفائز.")
                return True
            self.send_room_text(room, f"💎 فحص التنقيب — @{sender_name} يظهر الآن كأنه الفائز بالألماسة النادرة.")
            sent = self._send_rare_diamond_winner_card(room, sender_name)
            if not sent:
                self.send_room_text(room, "⚠️ تعذر إرسال صورة الفحص. تأكد من وجود صورة الألماسة النادرة داخل assets/treasures ومن إعداد رابط الوسائط العام.")
            return True

        if is_diamond_test:
            if not _is_configured_master_account(sender_name):
                self.send_room_text(room, "🔒 فحص الماس مخصص للماستر المحدد في متغيرات السيرفر فقط.")
                return True
            if not room:
                self.send_private_text(sender_name, "📌 أرسل أمر فحص الماس داخل الغرفة ليظهر الرد والصورة فيها.")
                return True
            result = MINING_STORE.give_test_diamond(user_id, sender_name)
            item = result.get("item") or {}
            self.send_room_text(
                room,
                f"💎 فحص الألماس — {sender_name}\n"
                f"{item.get('icon', '💎')} أُضيفت {item.get('name', 'ألماسة')} إلى مخزونك.\n"
                f"🛒 قيمة الاستبدال: {int(item.get('price', 0) or 0):,} نقطة للقطعة.",
            )
            image_path = ASSETS_DIR / "treasures" / str(item.get("image", ""))
            if not image_path.is_file():
                self.send_room_text(room, "⚠️ أُضيفت الألماسة لكن صورتها غير موجودة في مجلد assets/treasures.")
                return True
            image_url = self._game_public_image(image_path, route="assets/treasures")
            if not image_url:
                self.send_room_text(room, "⚠️ أُضيفت الألماسة لكن تعذر تجهيز رابط صورتها.")
                return True
            try:
                if self.send_room_media(room, image_url, "image") is False:
                    self.send_room_text(room, "⚠️ أُضيفت الألماسة لكن تعذر إرسال صورتها.")
            except Exception as exc:
                self.log("[MINING] diamond test image send failed:", repr(exc))
                self.send_room_text(room, "⚠️ أُضيفت الألماسة لكن تعذر إرسال صورتها.")
            return True

        settled = self._resume_pending_mining_redemption(room, sender_name, user_id)
        if settled is False:
            return True

        if is_store:
            lines = ["⛏️ متجر الكنوز — الأسعار بالنقاط"]
            rare_key = _rare_diamond_item_key()
            for key, item in MINING_ITEMS.items():
                code = " (RED)" if rare_key and str(key) == rare_key else ""
                lines.append(f"{item['icon']} {item['name']}{code}: {item['price']:,} نقطة")
            lines.extend(("للتنقيب: احفر", "للمخزون: sv", "لاستبدال أي كنز من المتجر: red رمز أو اسم الكنز ثم العدد (مثال: red ⚙️ 3 أو red ♦️ 3)", "لاستبدال الألماسة النادرة RED: red العدد"))
            self.send_room_text(room, "\n".join(lines))
            return True

        if is_inventory:
            inventory = MINING_STORE.inventory(user_id, sender_name)
            current_items = inventory.get("inventory", {}) if isinstance(inventory, dict) else {}
            if not isinstance(current_items, dict):
                current_items = {}
            language_en = self._effective_room_language(room) == "en"
            lines = [f"🎒 Inventory for @{sender_name}" if language_en else f"🎒 مخزون @{sender_name}"]
            owned = False
            rare_key = _rare_diamond_item_key()
            for key, item in MINING_ITEMS.items():
                count = int(current_items.get(key, 0) or 0)
                if count:
                    owned = True
                    code = " (RED)" if rare_key and str(key) == rare_key else ""
                    lines.append(f"{item.get('icon', '💎')} {item.get('name', key)}{code}: {count}")
            if rare_key:
                rare_item = MINING_ITEMS.get(rare_key, {})
                rare_item = rare_item if isinstance(rare_item, dict) else {}
                rare_count = int(current_items.get(rare_key, 0) or 0)
                rare_price = int(rare_item.get("price", 0) or 0)
                if language_en:
                    lines.append(f"🔴 Rare RED diamond: {rare_count} — redeem: red {rare_count} ({rare_price:,} points per item)")
                else:
                    lines.append(f"🔴 ألماسة نادرة RED: {rare_count} — الاستبدال: red {rare_count} ({rare_price:,} نقطة للقطعة)")
            else:
                lines.append("⚠️ RED is not linked to a rare diamond in the treasure catalog." if language_en else "⚠️ رمز RED غير مرتبط بألماسة نادرة في كتالوج الكنوز.")
            auction_game = getattr(self, "_auction_game", None)
            auction_items = auction_game.inventory(user_id, sender_name) if auction_game else []
            if auction_items:
                owned = True
                lines.append("🏺 World auction collectibles in diamond inventory:" if language_en else "🏺 مقتنيات المزاد العالمي ضمن مخزون الألماس:")
                for number, auction_item in enumerate(auction_items, 1):
                    if language_en:
                        item_name = auction_item.get("item_name_en") or auction_item.get("item_name_ar") or "Auction prize"
                    else:
                        item_name = auction_item.get("item_name_ar") or auction_item.get("item_name_en") or "جائزة المزاد"
                    short_id = str(auction_item.get("id") or "")[:8]
                    if auction_item.get("status") == "redeeming":
                        status_text = "redemption pending" if language_en else "الاسترداد قيد الاعتماد"
                        lines.append(f"{number}. {auction_item.get('icon', '💎')} {item_name} — {status_text}")
                    else:
                        price_text = "1% profit, 1% loss, 98% no change" if language_en else "الاسترداد بالحظ: 1% ربح، 1% خسارة، و98% دون تغيير"
                        command_text = f"   Redeem: red auction {short_id}" if language_en else f"   red auction {short_id}"
                        lines.append(f"{number}. {auction_item.get('icon', '💎')} {item_name} — {price_text}")
                        lines.append(command_text)
            if not owned:
                lines.append("No treasures yet. Type dig or join the rare diamond expedition." if language_en else "لا توجد كنوز بعد؛ جرّب أمر احفر أو شارك في تنقيب الألماسة النادرة.")
            found = inventory.get("diamonds_found", 0) if isinstance(inventory, dict) else 0
            lines.append(f"💎 Total diamonds found: {found}" if language_en else f"💎 إجمالي الألماس الذي عثرت عليه: {found}")
            lines.append("🔁 Redeem: red <item name or icon> <quantity> — e.g. red ⚙️ 3 or red ♦️ 3" if language_en else "🔁 للاستبدال: red رمز أو اسم أي كنز العدد — مثال: red ⚙️ 3 أو red ♦️ 3")
            lines.append("🔴 Rare RED diamond: red <quantity>" if language_en else "🔴 للألماسة النادرة RED: red العدد")
            self.send_room_text_chunks(room, "\n".join(lines))
            return True

        if is_top:
            rows = MINING_STORE.leaderboard(10)
            if not rows:
                self.send_room_text(room, "💎 توب المنقبين\nلا توجد اكتشافات ألماس بعد.")
                return True
            lines = ["💎 توب المنقبين"]
            for place, row in enumerate(rows, 1):
                mark = {1: "🥇", 2: "🥈", 3: "🥉"}.get(place, f"{place}.")
                lines.append(f"{mark} @{row['username']} — {row['diamonds_found']} ألماسة")
            self.send_room_text(room, "\n".join(lines))
            return True

        if redeem_match or red_code_match:
            if red_code_match:
                argument = str(red_code_match.group(1) or "").strip()
                if re.match(r"^(?:auction|مزاد)\s+", argument, re.I):
                    selection = argument
                    quantity = None
                else:
                    quantity_match = re.fullmatch(r"(.+?)\s+(\d+)", argument)
                    if quantity_match:
                        selection = quantity_match.group(1).strip()
                        quantity = int(quantity_match.group(2))
                    elif argument.isdigit():
                        selection = "red"
                        quantity = int(argument)
                    else:
                        selection = argument or "red"
                        quantity = 1
            else:
                selection = redeem_match.group(1).strip()
                selection = selection.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
                quantity = None
                if not re.match(r"^(?:auction|مزاد)\s+", selection, re.I):
                    quantity_match = re.fullmatch(r"(.+?)\s+(\d+)", selection)
                    if quantity_match:
                        selection = quantity_match.group(1).strip()
                        quantity = int(quantity_match.group(2))
            auction_redemption = re.fullmatch(r"(?:auction|مزاد)\s*(.*)", selection, re.I | re.S)
            if auction_redemption:
                redeemed = self._auction_game.redeem(
                    user_id, sender_name, auction_redemption.group(1).strip(), _apply_points_delta_once,
                )
                if redeemed.get("ok"):
                    prize = redeemed.get("item") or {}
                    language_en = self._effective_room_language(room) == "en"
                    outcome = str(redeemed.get("outcome") or redeemed.get("result") or "").casefold()
                    if outcome in {"neutral", "none", "no_change", "unchanged", "لا_شيء", "دون_تغيير"}:
                        result_kind = "neutral"
                    elif outcome in {"loss", "counterfeit", "خسارة", "مغشوشة"}:
                        result_kind = "loss"
                    elif outcome in {"win", "profit", "authentic", "ربح", "أصلية"}:
                        result_kind = "win"
                    else:
                        result_kind = "win" if redeemed.get("authentic") else "loss"
                    if language_en:
                        prize_name = prize.get("item_name_en") or prize.get("item_name_ar") or "Auction prize"
                        result_label = {"win": "profit (1%)", "loss": "loss (1%)", "neutral": "no profit or loss (98%)"}[result_kind]
                        points_line = f"💰 Balance change: {int(redeemed.get('points', 0) or 0):,} points." if result_kind != "neutral" else "💰 Your balance did not change."
                        response = (
                            f"✅ Redeemed {prize.get('icon', '💎')} {prize_name} from the auction.\n"
                            f"🎲 Luck result: {result_label}.\n{points_line}"
                        )
                    else:
                        prize_name = prize.get("item_name_ar") or prize.get("item_name_en") or "جائزة المزاد"
                        result_label = {"win": "ربح (1%)", "loss": "خسارة (1%)", "neutral": "لا ربح ولا خسارة (98%)"}[result_kind]
                        points_line = f"💰 تغير رصيدك بمقدار {int(redeemed.get('points', 0) or 0):,} نقطة." if result_kind != "neutral" else "💰 لم يتغير رصيدك."
                        response = (
                            f"✅ استبدلت {prize.get('icon', '💎')} {prize_name} من المزاد.\n"
                            f"🎲 نتيجة الحظ: {result_label}.\n{points_line}"
                        )
                    self.send_room_text(room, response)
                elif redeemed.get("pending"):
                    pending_text = (
                        "⌛ Redemption is saved and waiting for points approval. Retry the same red auction <number/code> command later; it will not credit twice."
                        if self._effective_room_language(room) == "en"
                        else "⌛ الاستبدال محفوظ وينتظر اعتماد النقاط؛ أعد الأمر نفسه red auction <رقم/رمز> لاحقاً، ولن تتكرر الإضافة."
                    )
                    self.send_room_text(room, pending_text)
                else:
                    if self._effective_room_language(room) == "en":
                        errors = {
                            "selector_required": "❌ Type the collectible code shown by sv, e.g. red auction 1a2b3c4d.",
                            "item_not_found": "📭 That collectible is not available to redeem. Type sv to see your inventory.",
                        }
                        fallback = "⚠️ The auction collectible could not be redeemed right now."
                    else:
                        errors = {
                            "selector_required": "❌ اكتب رمز السلعة الظاهر في sv، مثال: red auction 1a2b3c4d.",
                            "item_not_found": "📭 لم أجد هذه السلعة في مقتنيات المزاد القابلة للاستبدال. اكتب sv لعرضها.",
                        }
                        fallback = "⚠️ تعذر استبدال سلعة المزاد الآن."
                    self.send_room_text(room, errors.get(redeemed.get("error"), fallback))
                return True
            if str(selection).strip().casefold() == "red":
                rare_key = _rare_diamond_item_key()
                item_keys = [rare_key] if rare_key else []
            else:
                item_keys = resolve_item_keys(selection)
            if not item_keys:
                if str(selection).strip().casefold() == "red":
                    self.send_room_text(room, "❌ رمز RED غير مربوط بعنصر ألماسة نادرة في treasure_mining.py؛ أضف العنصر إلى كتالوج الكنوز أولًا.")
                else:
                    self.send_room_text(room, "❌ اسم الكنز غير معروف. اكتب متجر الكنوز لمعرفة الأسماء والأسعار.")
                return True
            reserved = MINING_STORE.reserve_redemption(
                user_id, sender_name, item_keys, quantity,
            )
            if not reserved.get("ok"):
                errors = {
                    "empty_inventory": "📭 لا تملك من هذا الكنز شيئًا لاستبداله.",
                    "insufficient_items": f"❌ الكمية غير متوفرة؛ المتاح {reserved.get('available', 0)}.",
                    "invalid_quantity": "❌ اكتب كمية موجبة صحيحة.",
                    "pending_redemption": "⌛ يجري اعتماد استبدال محفوظ؛ أعد المحاولة بعد لحظات.",
                }
                self.send_room_text(room, errors.get(reserved.get("error"), "❌ تعذر تنفيذ الاستبدال."))
                return True
            pending = reserved["pending"]
            credited = _apply_points_delta_once(
                sender_name, pending["points"],
                f"mining-redemption:{pending['id']}", "treasure mining redemption",
                repay_loan=True,
            )
            if not credited.get("ok"):
                self.send_room_text(room, "⚠️ حُجزت الكنوز للاستبدال بأمان، لكن تعذر اعتماد النقاط الآن. أعد Rdem لاحقًا؛ لن تُخصم الكنوز مرتين.")
                return True
            MINING_STORE.complete_redemption(user_id, pending["id"])
            item_names = ", ".join(
                f"{MINING_ITEMS[key]['icon']} {MINING_ITEMS[key]['name']} ×{count}"
                for key, count in pending["items"].items()
            )
            self.send_room_text(
                room,
                f"✅ تم استبدال {item_names} لـ@{sender_name}\n"
                f"💰 أضيف إلى رصيدك {pending['points']:,} نقطة.",
            )
            return True

        if is_dig:
            mining_key = _norm_user(sender_name)
            now = time.time()
            cooldown_lock = getattr(self, "_mining_cooldown_lock", None)
            if cooldown_lock is None:
                cooldown_lock = self._mining_cooldown_lock = threading.RLock()
            last_dig_data = getattr(self, "_mining_last_dig", None)
            if not isinstance(last_dig_data, dict):
                last_dig_data = self._mining_last_dig = {}
            with cooldown_lock:
                last_dig = float(last_dig_data.get(mining_key, 0) or 0)
                remaining_gate = 0.0 if _is_env_master_for_game_cooldown(sender_name) else 60.0 - (now - last_dig)
                if remaining_gate > 0:
                    self.send_room_text(room, f"⌛ {sender_name} يمكنك التنقيب مرة كل دقيقة. تبقّى {format_cooldown(int(remaining_gate) + 1)}.")
                    return True
                last_dig_data[mining_key] = now
            outcome = MINING_STORE.dig(user_id, sender_name)
            if not outcome.get("ok"):
                if outcome.get("error") == "cooldown":
                    remaining = max(60, int(outcome.get("remaining", 0) or 0))
                    self.send_room_text(room, f"⌛ {sender_name} يمكنك التنقيب مرة كل دقيقة. تبقّى {format_cooldown(remaining)}.")
                elif outcome.get("error") == "pending_redemption":
                    self.send_room_text(room, "⌛ لديك استبدال كنوز ينتظر التسوية؛ استخدم Rdem مرة أخرى بعد قليل.")
                return True
            try:
                _record_game(sender_name, "treasure_mining", 0, 0)
            except Exception as exc:
                self.log("[MINING] game stats update failed:", repr(exc))
            item = outcome.get("item")
            if not item:
                self.send_room_text(room, f"🪨 {sender_name} لم يعثر على شيء هذه المرة. حظًا أوفر في التنقيب القادم.")
                return True
            self.send_room_text(
                room,
                f"🎉 {sender_name} عثرت على:\n{item['icon']} {item['name']}\n"
                "🎒 أُضيف إلى مخزونك.\n"
                f"🛒 قيمة الاستبدال: {int(item['price']):,} نقطة للقطعة.",
            )
            image_path = ASSETS_DIR / "treasures" / item.get("image", "")
            if image_path.is_file():
                image_url = self._game_public_image(image_path, route="assets/treasures")
                if image_url:
                    try:
                        self.send_room_media(room, image_url, "image")
                    except Exception as exc:
                        self.log("[MINING] treasure image send failed:", repr(exc))
            return True
        return False

    def _payroll_command(self, room, username, command):
        """Require a fresh random job before every salary; persist cooldowns and claims."""
        username = str(username or "").strip().lstrip("@")
        key = _norm_user(username)
        if not key:
            return False
        english = getattr(self, "_configured_room_language", lambda _room: "ar")(room) == "en"
        tr = lambda ar, en: en if english else ar
        lock = _PAYROLL_LOCK
        now = time.time()
        try:
            with lock:
                data = _load_local_json(PAYROLL_FILE, {"version": 1, "users": {}})
                users = data.get("users", {}) if isinstance(data, dict) else {}
                users = dict(users) if isinstance(users, dict) else {}
                state = dict(users.get(key, {})) if isinstance(users.get(key), dict) else {}
                pending = state.get("pending") if isinstance(state.get("pending"), dict) else None

                if command == "work":
                    if pending:
                        job_name = PAYROLL_JOBS_EN.get(str(pending.get("job") or ""), pending.get("job")) if english else pending.get("job")
                        self.send_room_text(room, tr(
                            f"👷 وظيفتك الحالية: {job_name} — راتبك {_fmt_points(pending.get('amount', 0))} نقطة. اكتب راتبي لاستلامه.",
                            f"👷 Your current job is {job_name}; salary: {_fmt_points(pending.get('amount', 0))} points. Type salary to collect it.",
                        ))
                        return True
                    last_work = float(state.get("last_work_at") or 0)
                    remaining = PAYROLL_COOLDOWN_SECONDS - (now - last_work)
                    if last_work and remaining > 0:
                        seconds = max(1, int(remaining + 0.999))
                        self.send_room_text(room, tr(f"⏳ استرح قبل العمل التالي. يمكنك العمل بعد {seconds} ثانية.", f"⏳ Please wait {seconds} seconds before your next job."))
                        return True
                    job, amount = secrets.choice(PAYROLL_JOBS)
                    shown_job = PAYROLL_JOBS_EN.get(job, job) if english else job
                    pending = {"id": uuid.uuid4().hex, "job": job, "amount": int(amount), "created_at": int(now)}
                    state.update(username=username, last_work_at=now, pending=pending)
                    users[key] = state
                    _save_local_json(PAYROLL_FILE, {"version": 1, "users": users})
                    self.send_room_text(room, tr(
                        f"👷 حصلت على وظيفة: {shown_job}\n💰 راتبك: {_fmt_points(amount)} نقطة\n📩 اكتب راتبي لاستلامه. يتجدد العمل كل 10 دقائق.",
                        f"👷 Random job assigned: {shown_job}\n💰 Salary: {_fmt_points(amount)} points\n📩 Type salary to collect it. Work refreshes every 10 minutes.",
                    ))
                    return True

                if not pending:
                    self.send_room_text(room, tr("❌ لا يوجد راتب جاهز. أرسل عمل أولاً لتحصل على وظيفة عشوائية.", "❌ No salary is ready. Type work first to get a random job."))
                    return True
                amount = int(pending.get("amount") or 0)
                job = str(pending.get("job") or "وظيفة")
                shown_job = PAYROLL_JOBS_EN.get(job, job) if english else job
                txid = "payroll:" + str(pending.get("id") or "")
                result = _apply_points_delta_once(
                    username, amount, txid, "work salary", repay_loan=True
                )
                self._notify_loan_repayment(username, result)
                if not result.get("ok"):
                    self.send_room_text(room, tr("⚠️ تعذر حفظ الراتب الآن؛ بقي راتبك محفوظًا. أعد كتابة راتبي بعد قليل.", "⚠️ Salary could not be saved. Your claim remains pending; type salary again shortly."))
                    return True
                state.pop("pending", None)
                users[key] = state
                _save_local_json(PAYROLL_FILE, {"version": 1, "users": users})
            self.send_room_text(room, tr(
                f"💵 تم صرف راتبك يا @{username}\n👷 الوظيفة: {shown_job}\n💰 الراتب: +{_fmt_points(amount)} نقطة\n💳 رصيدك: {_fmt_points(result.get('balance', 0))} نقطة",
                f"💵 Salary paid to @{username}\n👷 Job: {shown_job}\n💰 Pay: +{_fmt_points(amount)} points\n💳 Balance: {_fmt_points(result.get('balance', 0))} points",
            ))
            return True
        except Exception as exc:
            self.log("[PAYROLL] operation failed:", username, repr(exc))
            self.send_room_text(room, tr("⚠️ تعذر إكمال عملية الراتب؛ لم أؤكد الصرف. حاول مرة أخرى بعد قليل.", "⚠️ Salary processing failed; payment was not confirmed. Please retry shortly."))
            return True

    def _save_stock_investments(self):
        with self._stock_market_lock:
            snapshot = [dict(row) for row in self.stock_investments if isinstance(row, dict)]
        _save_local_json(STOCK_MARKET_FILE, {"version": 1, "investments": snapshot})

    def _stock_history_command(self, room, sender):
        """Send a user's full retained stock-market history in private, Ns pages."""
        user_key = _norm_user(sender)
        with self._stock_market_lock:
            rows = [dict(row) for row in getattr(self, "stock_market_history", [])
                    if isinstance(row, dict) and _norm_user(row.get("username")) == user_key]
            seen_ids = {str(row.get("id")) for row in rows}
            rows.extend(dict(row) for row in getattr(self, "stock_investments", [])
                         if isinstance(row, dict) and _norm_user(row.get("username")) == user_key
                         and str(row.get("id")) not in seen_ids)
        rows.sort(key=lambda row: float(row.get("settled_at", row.get("started_at", 0)) or 0), reverse=True)
        english = self._configured_room_language(room) == "en"
        if english:
            title = f"📈 My stock-market history — {len(rows)} investments"
        else:
            title = f"📈 سجل بورصتي — {len(rows)} عملية"
        if not rows:
            self.send_private_text(sender, "📭 لا توجد استثمارات بورصة مسجلة لحسابك بعد." if not english else "📭 You have no recorded stock-market investments yet.")
            return True

        details = []
        for row in rows:
            try:
                asset_id = int(row.get("asset", 0) or 0)
            except (TypeError, ValueError):
                asset_id = 0
            asset = STOCK_MARKET_ASSETS.get(asset_id, {})
            asset_name = asset.get("en" if english else "ar", "Unknown" if english else "غير معروف")
            when_ts = row.get("settled_at", row.get("started_at", row.get("timestamp", 0)))
            try:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(when_ts or 0)))
            except (TypeError, ValueError, OverflowError, OSError):
                when = "unknown time" if english else "وقت غير معروف"
            amount = int(row.get("amount", 0) or 0)
            status = str(row.get("status") or "settled")
            room_name = _transfer_log_safe_field(row.get("room"), 36)
            if status in {"pending_debit", "active", "paying"}:
                line = (f"{when} | {asset.get('icon', '📈')} {asset_name} | {_fmt_points(amount)} | "
                        f"{'Pending settlement' if english else 'قيد التسوية'} | {room_name}")
            else:
                outcome = str(row.get("outcome") or "profit")
                change = int(row.get("change", row.get("profit", 0)) or 0)
                is_profit = outcome == "profit"
                label = (("Profit" if is_profit else "Loss") if english
                         else ("ربح" if is_profit else "خسارة"))
                sign = "+" if is_profit else "-"
                line = (f"{when} | {asset.get('icon', '📈')} {asset_name} | {_fmt_points(amount)} | "
                        f"{label} ({sign}{_fmt_points(change)}) | {room_name}")
            details.append(_transfer_log_safe_field(line, max_bytes=170))

        pages = []
        for start in range(0, len(details), 3):
            page_no = start // 3 + 1
            page_total = (len(details) + 2) // 3
            header = (f"📈 My stock-market history — List {page_no}/{page_total}"
                      if english else f"📈 سجل بورصتي — القائمة {page_no}/{page_total}")
            pages.append("\n".join((header, "━━━━━━━━━━━━", *details[start:start + 3])))
        page_key = ("chat_message", "", str(sender or ""))
        result_pages = getattr(self, "_result_pages", {})
        if not isinstance(result_pages, dict):
            result_pages = {}
        result_pages[page_key] = {
            "pages": pages, "part": 1, "kwargs": {"to": str(sender or "")},
            "language": "en" if english else "ar",
        }
        self._result_pages = result_pages
        first = pages[0]
        if len(pages) > 1:
            first += "\n\n📌 Type ns for the next list." if english else "\n\n📌 اكتب ns للقائمة التالية."
        else:
            result_pages.pop(page_key, None)
            first += "\n\n✅ End of list." if english else "\n\n✅ انتهت القوائم."
        self.send_private_text(sender, first)
        return True
