"""English room-message translation rules and English message catalog."""

class EnglishRoomMessagesSection:
    def _translate_english_room_message(self, text):
        """Translate recognized Arabic room messages to English."""
        value = str(text or "")
        # English text needs no processing. Arabic rooms returned above already;
        # therefore translation work is limited to Arabic-bearing text in English rooms.
        if not any("\u0600" <= char <= "\u06ff" for char in value):
            return value
        cached = getattr(self, "_english_room_exact_translations", None)
        if cached is None:
            cached = _load_english_room_messages()
            self._english_room_exact_translations = cached
        exact = cached
        if value in exact:
            return exact[value]
        cricket_start = re.fullmatch(
            r"🏏 بدأت مباراة الكريكيت\n(.+)\n(.+)\n🎯 يبدأ الهجوم: (.+)\. لكل لاعب 6 كرات في كل دور\.",
            value,
        )
        if cricket_start:
            def translate_team(line):
                for arabic, english in (
                    ("👥 الهجوم — ", "👥 Attack — "), ("👥 الدفاع — ", "👥 Defense — "),
                    ("🤖 الهجوم — ", "🤖 Attack — "), ("🤖 الدفاع — ", "🤖 Defense — "),
                    (" لاعبين): ", " players): "), (" لاعبان): ", " players): "),
                    ("لاعبون قيد الانضمام", "players joining"),
                    ("الفريق الأول", "the first team"), ("الفريق الثاني", "the second team"),
                    ("الفريق", "team"),
                ):
                    line = line.replace(arabic, english)
                return line.replace("، ", ", ")
            batting = "Attack" if cricket_start.group(3) == "الهجوم" else "Defense"
            return (
                "🏏 Cricket match started\n" + translate_team(cricket_start.group(1)) + "\n"
                + translate_team(cricket_start.group(2))
                + f"\n🎯 Batting first: {batting}. Each player has 6 balls per innings."
            )
        # Ludo has dynamic player names and scores; translate full message
        # templates while preserving those values exactly.
        match = re.fullmatch(r"🎲 تم اختيار لودو لـ (\d+) لاعبين\.\n👥 اكتب join للانضمام — نحتاج (\d+) لاعباً إضافياً\.\n📌 يبدأ اللعب بعد اكتمال العدد، ثم اكتب rool\.", value)
        if match:
            return f"🎲 Ludo set for {match.group(1)} players.\n👥 Type join to enter — {match.group(2)} more player(s) needed.\n📌 The game starts when all players join; type rool on your turn to roll."
        match = re.fullmatch(r"🎮 لديك لعبة لودو شغالة بالفعل في غرفة: (.+)\n✏️ اكتب اسم اللعبة لبدء لعبة جديدة\.", value)
        if match:
            return f"🎮 You already have a Ludo game running in {match.group(1)}.\n✏️ Type ludo to start a new game."
        match = re.fullmatch(r"🎲 توجد لعبة لودو شغالة بالفعل في غرفة: (.+)\. اختر العدد أو اكتب join/انضمام\.", value)
        if match:
            return f"🎲 A Ludo game is already running in {match.group(1)}. Choose the player count or type join."
        match = re.fullmatch(r"🤖 بدأت لعبة لودو مع البوت! أنت تبدأ أولاً، اكتب rool للعب\.", value)
        if match:
            return "🤖 Ludo vs. the bot has started! You go first. Type rool to roll the die."
        match = re.fullmatch(r"🎲 بدأت لعبة لودو!\n👥 اللاعبون: (.+)\n🎯 دور (.+)، اكتب rool\.", value)
        if match:
            return f"🎲 Ludo has started!\n👥 Players: {match.group(1)}\n🎯 {match.group(2)} goes first — type rool to roll."
        match = re.fullmatch(r"✅ انضم اللاعب\.\n👥 باقي (\d+) لاعب/لاعبين ثم تبدأ اللعبة\.", value)
        if match:
            return f"✅ Player joined.\n👥 {match.group(1)} more player(s) needed before the game starts."
        match = re.fullmatch(r"🎲 @(.+?) وقف الرول على (\d+) وانتقل من المربع (\d+) إلى (\d+)\.", value)
        if match:
            return f"🎲 @{match.group(1)} rolled {match.group(2)} and moved from space {match.group(3)} to {match.group(4)}."
        match = re.fullmatch(r"🤖 البوت رمى (\d+) وانتقل من المربع (\d+) إلى (\d+)\.\n🎯 الآن دور @(.+)، اكتب rool\.", value)
        if match:
            return f"🤖 The bot rolled {match.group(1)} and moved from space {match.group(2)} to {match.group(3)}.\n🎯 @{match.group(4)}, it is your turn — type rool."
        match = re.fullmatch(r"🎯 الآن دور @(.+)، اكتب rool\.", value)
        if match:
            return f"🎯 @{match.group(1)}, it is your turn — type rool to roll."
        match = re.fullmatch(r"🏆 مبروك! فاز @(.+?) بلعبة لودو\.\n🎲 الرول الأخير: (\d+)\n📍 وصل إلى نهاية المسار\.\n💰 جائزة الفوز: \+([\d,]+) نقطة\n💳 رصيده الآن: ([\d,]+) نقطة", value)
        if match:
            return f"🏆 Congratulations, @{match.group(1)} won Ludo!\n🎲 Final roll: {match.group(2)}\n📍 Reached the finish.\n💰 Prize: +{match.group(3)} points\n💳 New balance: {match.group(4)} points"
        match = re.fullmatch(r"🛡️ حماية الغرفة شغالة\. حد التكرار: (\d+) رسائل\.", value)
        if match:
            return f"🛡️ Room protection is enabled. Repetition limit: {match.group(1)} messages."
        match = re.fullmatch(r"🛡️ تم تشغيل حماية الغرفة من السب\.|⛔ تم إيقاف حماية الغرفة من السب\.|🛡️ تم تشغيل حماية الغرفة من الفلود\.|⛔ تم إيقاف حماية الغرفة من الفلود\.|🛡️ تم تشغيل حماية الدخول والخروج\.|⛔ تم إيقاف حماية الدخول والخروج\.|🛡️ تم تشغيل حظر الحسابات بلا صورة\.|⛔ تم إيقاف حظر الحسابات بلا صورة\.", value)
        if match:
            protection_messages = {
                "🛡️ تم تشغيل حماية الغرفة من السب.": "🛡️ Profanity protection enabled.",
                "⛔ تم إيقاف حماية الغرفة من السب.": "⛔ Profanity protection disabled.",
                "🛡️ تم تشغيل حماية الغرفة من الفلود.": "🛡️ Flood protection enabled.",
                "⛔ تم إيقاف حماية الغرفة من الفلود.": "⛔ Flood protection disabled.",
                "🛡️ تم تشغيل حماية الدخول والخروج.": "🛡️ Repeated join/leave protection enabled.",
                "⛔ تم إيقاف حماية الدخول والخروج.": "⛔ Repeated join/leave protection disabled.",
                "🛡️ تم تشغيل حظر الحسابات بلا صورة.": "🛡️ Blocking accounts without a profile photo enabled.",
                "⛔ تم إيقاف حظر الحسابات بلا صورة.": "⛔ Blocking accounts without a profile photo disabled.",
            }
            return protection_messages[value]
        match = re.fullmatch(r"✅ تم ضبط حماية التكرار في (.+) على (\d+) رسائل متتالية\.", value)
        if match:
            return f"✅ Repetition protection for {match.group(1)} is set to {match.group(2)} consecutive messages."
        match = re.fullmatch(r"✅ تم اعتماد حد الفلود: (\d+) رسائل متكررة في الغرفة: (.+)", value)
        if match:
            return f"✅ Flood limit set to {match.group(1)} repeated messages in room {match.group(2)}."
        match = re.fullmatch(r"🐎🛡️ @(.+?) حصل على حصانة!\n━━━━━━━━━━━━\n⏱️ الحماية من السرقة: دقيقة واحدة\n🔓 تنتهي بعد 60 ثانية\.\n━━━━━━━━━━━━", value)
        if match:
            return f"🐎🛡️ @{match.group(1)} is protected from stealing for one minute.\n🔓 Protection expires in 60 seconds."
        match = re.fullmatch(r"⌛ انتهت لعبة (.+) تلقائياً لعدم وجود تفاعل لمدة دقيقتين\.", value)
        if match:
            return f"⌛ {match.group(1)} ended automatically after two minutes without activity."
        match = re.fullmatch(r"⏳ @(.+?) انتظر (.+) قبل بدء لعبة (.+)\.\n🎮 الفاصل بين لعبتي (.+) لنفس اللاعب هو 4 دقائق\.", value)
        if match:
            return f"⏳ @{match.group(1)}, wait {match.group(2)} before starting {match.group(3)} again.\n🎮 The cooldown for the same player and game is 4 minutes."
        match = re.fullmatch(r"⏳ @(.+?) لديك لعبة (.+) شغالة حالياً\.\n🎯 ما زالت تبحث عن منافس\.\n⌛ مدة البحث دقيقتان فقط، وبعدها تُلغى تلقائياً إذا لم يدخل منافس\.", value)
        if match:
            return f"⏳ @{match.group(1)}, your {match.group(2)} game is already waiting.\n🎯 Looking for an opponent.\n⌛ It will be cancelled after two minutes if nobody joins."
        match = re.fullmatch(r"❌ تحتاج (.+) نقطة للمشاركة في (.+)\. رصيدك: (.+)", value)
        if match:
            return f"❌ You need {match.group(1)} points to play {match.group(2)}. Your balance: {match.group(3)}."
        match = re.fullmatch(r"🌱 تم زرع (.+) بنجاح!\n⏱️ الانتظار: (.+) دقيقة\n🎁 المكافأة: (.+) نقطة\n📩 عند اكتمال الزراعة ستصلك النتيجة تلقائياً في الخاص\.", value)
        if match:
            return f"🌱 {match.group(1)} planted successfully!\n⏱️ Growing time: {match.group(2)} minutes\n🎁 Reward: {match.group(3)} points\n📩 You'll receive the result in a private message when it's ready."
        match = re.fullmatch(r"❌ رصيدك غير كافٍ\. رصيدك: (.+)", value)
        if match:
            return f"❌ Not enough points. Your balance: {match.group(1)}."
        match = re.fullmatch(r"⏰ @(.+?) انتهى وقت الاختيار\. أرسل أمر اللعبة من جديد\.", value)
        if match:
            return f"⏰ @{match.group(1)}, the selection timed out. Send the game command again."
        match = re.fullmatch(r"❌ لم أجد @(.+?) ضمن أعضاء الغرفة\.", value)
        if match:
            return f"❌ I couldn't find @{match.group(1)} among the room members."
        match = re.fullmatch(r"🎯 تحدي @(.+?): (.+)", value)
        if match:
            return f"🎯 Challenge for @{match.group(1)}: {match.group(2)}"
        match = re.fullmatch(r"🏆 فاز @(.+?) بلعبة السلم والثعبان!\n🎲 الرول الأخير: (\d+)\n📍 وصل إلى الخانة 100\.\n💰 جائزة الفوز: \+([\d,]+) نقطة\n💳 رصيده الآن: ([\d,]+) نقطة", value)
        if match:
            return f"🏆 @{match.group(1)} won Snake & Ladders!\n🎲 Final roll: {match.group(2)}\n📍 Reached square 100.\n💰 Prize: +{match.group(3)} points\n💳 New balance: {match.group(4)} points"
        match = re.fullmatch(r"🏆 انتهت لعبة (.+)\n👑 الفائز: @(.+)\n💰 مبلغ الفوز: \+(.+) نقطة\n❌ الخاسر: @(.+)\n💸 مبلغ الخسارة: -(.+) نقطة", value)
        if match:
            return f"🏆 {match.group(1)} is over.\n👑 Winner: @{match.group(2)}\n💰 Winnings: +{match.group(3)} points\n❌ Loser: @{match.group(4)}\n💸 Loss: -{match.group(5)} points"
        match = re.fullmatch(r"🍉 فيس @(.+)\n🤖 البوت أرسل (.+)\n❌ لم تتم المطابقة، حظاً موفقاً\.\n💰 مبلغ الفوز: 0 نقطة\n💸 مبلغ الخسارة: 0 نقطة", value)
        if match:
            return f"🍉 Face match — @{match.group(1)}\n🤖 The bot chose {match.group(2)}\n❌ No match this time. Better luck next time!\n💰 Winnings: 0 points\n💸 Loss: 0 points"
        match = re.fullmatch(r"📈💰 البورصة\n━━━━━━━━━━━━━━\n👤 اللاعب: @(.+)\n(.+) الاختيار: (.+)\n(.+)\n🎁 المكافأة: \+(.+) نقطة\n💰 الرصيد: (.+)", value)
        if match:
            return f"📈💰 Stock market\n━━━━━━━━━━━━━━\n👤 Player: @{match.group(1)}\n{match.group(2)} Choice: {match.group(3)}\n{match.group(4)}\n🎁 Reward: +{match.group(5)} points\n💰 Balance: {match.group(6)}"
        match = re.fullmatch(r"📊✨ استثمار مع البوت\n━━━━━━━━━━━━\n👤 اللاعب: @(.+)\n(.+)\n🎁 المكافأة: \+(.+) نقطة\n💰 الرصيد: (.+)", value)
        if match:
            return f"📊✨ Investment with the bot\n━━━━━━━━━━━━\n👤 Player: @{match.group(1)}\n{match.group(2)}\n🎁 Reward: +{match.group(3)} points\n💰 Balance: {match.group(4)}"
        match = re.fullmatch(r"🕵️💰 تمت السرقة بنجاح!\n👤 السارق: @(.+)\n🎯 الضحية: @(.+)\n💸 المسروق: 500 نقطة\n🎁 @(.+) حصل على \+500 نقطة\.\n💰 رصيد السارق: (.+)", value)
        if match:
            return f"🕵️💰 The steal succeeded!\n👤 Thief: @{match.group(1)}\n🎯 Victim: @{match.group(2)}\n💸 Stolen: 500 points\n🎁 @{match.group(3)} received +500 points.\n💰 Thief's balance: {match.group(4)}"
        match = re.fullmatch(r"@(.+) لديه حصانه من السرقه", value)
        if match:
            return f"@{match.group(1)} is protected from being robbed."
        match = re.fullmatch(r"❌ رصيدك غير كافٍ\. رصيدك: (.+)", value)
        if match:
            return f"❌ Not enough points. Your balance: {match.group(1)}."
        match = re.fullmatch(r"🛡️ حماية الغرفة شغالة\. حد التكرار: (\d+) رسائل\.", value)
        if match:
            return f"🛡️ Room protection is enabled. Repetition limit: {match.group(1)} messages."
        match = re.fullmatch(r"(🛡️ تم تشغيل|⛔ تم إيقاف) حماية (.+)\.\n🏠 الغرفة: (.+)", value)
        if match:
            state = "enabled" if match.group(1).startswith("🛡️") else "disabled"
            label = {
                "حماية الغرفة من السب": "profanity protection",
                "حماية الغرفة من الفلود": "flood protection",
                "حماية الدخول والخروج": "repeated join/leave protection",
                "حظر الحسابات بلا صورة": "blocking accounts without a profile photo",
            }.get(match.group(2), match.group(2))
            return f"{match.group(1)[:2]} {label.capitalize()} {state}.\n🏠 Room: {match.group(3)}"
        match = re.fullmatch(r"🚫 \[حماية الفلود\] تم حظر IP سريعاً للحسابات التالية: (.+)", value)
        if match:
            return f"🚫 [Flood protection] IP-banned the following accounts after a rapid join attack: {match.group(1)}"
        match = re.fullmatch(r"🔢 أرسل عدد الرسائل المتكررة المسموح بها قبل الحظر \(من 2 إلى 50\)\.", value)
        if match:
            return "🔢 Send the allowed number of repeated messages before a ban (2–50)."
        # Keep names, numbers and URLs intact while translating the most common labels.
        cached = getattr(self, "_english_room_replacements", None)
        if cached is None:
            cached = {
                "تم تشغيل الأغنية": "Song started", "الأغنية": "song", "في البث": "in the live broadcast",
                "جاري تجهيز طلب الأغنية": "Preparing the song request", "تم نشر الصورة بنجاح": "Image posted successfully",
                "أوامر البوت": "Bot commands", "اختر لغة البوت للغرف المحددة": "Choose the bot language for the selected rooms",
                "العربية": "Arabic", "عدد الغرف": "Rooms", "أرسلت طلبات الدخول": "Join requests sent",
                "انتظر تأكيد الخادم": "Wait for server confirmation", "تم قبول طلب دخول الغرفة": "Room join request accepted",
                "وداعاً نلتقي مرة أخرى": "Goodbye, see you again", "مرحبا لقد عدت ادري وحشتكم": "Hello, I am back; I know you missed me",
                "لعبة العملة": "Coin toss", "اختيارك:": "Your choice:", "النتيجة:": "Result:",
                "وجه": "Heads", "كتابة": "Tails", "فزت!": "You won!", "لم تفز هذه المرة.": "You didn't win this time.",
                "عجلة الحظ": "Lucky wheel", "دارت العجلة وتوقفت على:": "The wheel landed on:",
                "مبلغ الفوز:": "Winnings:", "مبلغ الخسارة:": "Loss:", "رصيدك:": "Your balance:",
                "الرصيد:": "Balance:", "نقطة": "points", "نقاط": "points", "المكافأة:": "Reward:",
                "لعبة الصناديق": "Box game", "اخترت الصندوق": "You chose box", "الصندوق الرابح:": "Winning box:",
                "الصندوق الرابح كان:": "The winning box was:", "ربحت!": "You won!", "لم تربح.": "You didn't win.",
                "أرسل الرقم فقط": "Send the number only", "صندوق ": "Box ",
                "لعبة الكؤوس": "Cup game", "اختر الكوب:": "Choose a cup:", "اخترت الكوب": "You chose cup",
                "الكأس الصحيح:": "Correct cup:", "وجدت الجائزة!": "You found the prize!", "الجائزة كانت في الكوب:": "The prize was under cup:", "الكوب الخطأ.": "Wrong cup.",
                "معركة الوحش": "Monster battle", "قوتك:": "Your strength:", "قوة الوحش:": "Monster strength:",
                "هزمت الوحش!": "You defeated the monster!", "الوحش هزمك.": "The monster defeated you.", "تعادل مع الوحش — لا توجد جائزة.": "It's a tie with the monster — no prize.",
                "لعبة البركان": "Volcano game", "خرجت الجائزة من البركان!": "The prize erupted from the volcano!", "انفجر البركان ولم تجد جائزة.": "The volcano erupted, but there was no prize.",
                "صيد الطائر": "Bird hunt", "ظهر:": "Appeared:", "عصفور": "Sparrow", "نسر": "Eagle", "بومة": "Owl", "ببغاء": "Parrot", "لم يظهر طائر": "No bird appeared",
                "لعبة النجمة": "Star game", "حظك:": "Your luck:", "عادية": "Regular", "لامعة": "Shiny", "نادرة": "Rare", "أسطورية": "Legendary", "لم تلتقط نجماً": "You caught no star",
                "بنك مليون": "Million Bank", "جاري البحث عن الجائزة...": "Searching for the prize...", "جاري البحث عن مليار...": "Searching for the billion...",
                "🏆 فزت بـ ": "🏆 You won ", "🤖 البوت فاز!": "🤖 The bot won!", "🤝 تعادل!": "🤝 It's a tie!",
                "طاولة": "Table game", "أونو": "UNO", "أنت:": "You:", "البوت:": "Bot:",
                "اللاعب:": "Player:", "الاختيار:": "Choice:", "لا تتم المطابقة، حظاً موفقاً.": "No match. Better luck next time.",
                "تعذر": "Could not", "لا يوجد": "No", "انتهت": "Ended", "انتظر دورك": "Please wait for your turn",
                "تحتاج ": "You need ", "للمشاركة في ": " to play ", "رصيدك غير كافٍ": "Not enough points",
                "حصل على حصانة": "is protected", "جاري البحث عن الضحية...": "Searching for a target...",
                "حاول سرقة": "tried to rob", "فشلت السرقة": "The robbery failed", "المسروق": "the target",
                "مفلس.": "has no points.", "رصيده:": "Their balance:", "تم إبلاغ الشرطة!": "The police were notified!",
                "لم تتم السرقة.": "The robbery failed.", "تمت السرقة بنجاح!": "The robbery succeeded!",
                "السارق:": "Thief:", "الضحية:": "Victim:", "المسروق:": "Stolen:", "حصل على +": "received +",
                "رصيد السارق:": "Thief's balance:", "المستخدم:": "User:", "لا يوجد عضو آخر متاح": "No other eligible member is available",
                "انضم اللاعب.": "Player joined.", "باقي ": "", "لاعبين ثم تبدأ اللعبة.": " players needed before the game starts.",
                "بدأت اللعبة": "The game started", "اللاعبون:": "Players:", "دور ": "Turn: ", "اللاعب": "Player",
                "أرسل رقم الخيار": "Send the option number", "انتهى وقت الاختيار.": "Selection timed out.",
                "الاختيار: ": "Choice: ", "النتيجة: ": "Result: ", "غير صحيح.": "Invalid.",
                "لم تتم المطابقة": "No match", "تمت المطابقة": "It's a match", "حظاً موفقاً": "Better luck next time",
                "مبروك": "Congratulations", "فاز": "won", "فائز": "Winner", "الخاسر": "Loser",
                "ربحت": "You won", "خسرت": "You lost", "نجحت وربحت": "You succeeded and won",
                "لم تنجح هذه المرة.": "You didn't win this time.", "اختر": "Choose", "الطريقة:": "How to play:",
                "الانتظار:": "Wait:", "دقيقة واحدة": "one minute", "دقائق": "minutes", "دقيقة": "minute",
                "ثانية": "seconds", "ثواني": "seconds", "قبل بدء لعبة": "before starting the game",
                "المستثمر:": "Investor:", "الربح:": "Profit:", "الخسارة:": "Loss:", "استثمار مع البوت": "Bot investment",
                "🏏 المهاجم @": "🏏 Attacker @", "🛡️ المدافع @": "🛡️ Defender @", " أرسل رقم ": " sent: ", " لمحاولة الصد": " (block attempt)",
                "💡 عند اكتمال الزراعة تظهر المكافأة تلقائياً في الغرفة التي زرعت فيها.": "💡 When ready, the reward is posted in the room where you planted.",
                "📢 عند اكتمالها ستظهر النتيجة في هذه الغرفة، ولن تصلك رسالة خاصة.": "📢 When it finishes, the result appears in this room—not in a private message.",
                "حياة سعيدة مليئة بالفرح.": "a happy life full of joy.", "نتمنى لكما": "Wishing you both",
                "لم أجد عضواً آخر متاحاً للزواج حالياً.": "I couldn't find another available player for a wedding right now.",
                "لم أجد عضواً آخر حاضراً في هذه الغرفة.": "I couldn't find another player in this room.",
                "الشخصية المشهورة:": "Celebrity:", "لغز ": "Riddle ", "الإجابة:": "Answer:",
                "الجواب:": "Answer:", "انتهى وقت الاختيار.": "Selection time is over.",
                "طلبت:": "You asked for:", "فشلت": "Failed", "نجحت": "Succeeded", "انتهت لعبة": "Game ended:",
                "المبلغ غير صحيح.": "Invalid amount.", "لاعباً": "players", "لاعبين": "players", "مع البوت": "against the bot",
                "ابدأ": "Start", "اكتب rool": "type rool", "اكتب join": "type join",
                "بدأت لعبة السلم والثعبان! جاري البحث عن خصم. للمشاركة اكتب join": "Snake & Ladders started! Waiting for an opponent. Type join to participate.",
                "وقف الرول على": "rolled", "وانتقل من": "and moved from", "إلى": "to",
                "بلعبة السلم والثعبان!": "won Snake & Ladders!", "الرول الأخير:": "Final roll:",
                "وصل إلى الخانة 100.": "Reached square 100.", "رصيده الآن:": "New balance:",
                "🎮 لديك لعبة السلم والثعبان شغالة بالفعل في غرفة:": "🎮 You already have a Snake & Ladders game running in room:",
                "🐍 توجد لعبة السلم والثعبان شغالة بالفعل في غرفة:": "🐍 A Snake & Ladders game is already running in room:",
            }
            self._english_room_replacements = cached
        replacements = cached
        for ar, en in replacements.items():
            value = value.replace(ar, en)
        # Mining and Cricket responses are also delivered through this central
        # path, so English rooms receive localized command output regardless
        # of whether the user sent the Arabic or English command alias.
        mining_and_cricket_replacements = {
            "⛏️ متجر الكنوز — الأسعار بالنقاط": "⛏️ Treasure shop — prices in points",
            "للتنقيب: احفر": "To mine: dig",
            "للمخزون: sv": "For inventory: sv or inventory",
            "لاستبدال أي كنز من المتجر:": "To redeem any treasure:",
            "لاستبدال الألماسة النادرة RED:": "To redeem the rare RED diamond:",
            "🎒 مخزون @": "🎒 Inventory for @",
            "🔴 ألماسة نادرة RED:": "🔴 Rare RED diamond:",
            "— الاستبدال:": "— redemption:",
            "⚠️ رمز RED غير مرتبط بألماسة نادرة في كتالوج الكنوز.": "⚠️ RED is not linked to a rare diamond in the treasure catalog.",
            "لا توجد كنوز بعد؛ جرّب أمر احفر أو شارك في تنقيب الألماسة النادرة.": "No treasures yet. Type dig or join the rare diamond expedition.",
            "💎 إجمالي الألماس الذي عثرت عليه:": "💎 Total diamonds found:",
            "🔁 للاستبدال:": "🔁 To redeem:",
            "🔴 للألماسة النادرة RED:": "🔴 For the rare RED diamond:",
            "💎 توب المنقبين": "💎 Diamond leaderboard",
            "لا توجد اكتشافات ألماس بعد.": "No diamonds have been found yet.",
            "ألماس ملكي": "Royal diamond", "الألماس الملكي": "Royal diamonds",
            "الألماس البنفسجي": "Purple diamonds", "الألماس الأزرق": "Blue diamonds",
            "الألماس الأحمر": "Red diamonds", "الألماس الأسود": "Black diamonds",
            "الألماس العادي": "Clear diamonds", "ألماسة نادرة": "rare diamond",
            "ألماسة": "diamond", "ألماس": "diamonds", "الياقوت": "Ruby",
            "النحاس": "Copper", "الحديد": "Iron", "أُضيفت ": "Added ",
            "أُضيف إلى مخزونك": "Added to your inventory", "عثرت على:": "found:",
            "لم يعثر على شيء هذه المرة. حظًا أوفر في التنقيب القادم.": "found nothing this time. Better luck on the next dig.",
            "يمكنك التنقيب مرة كل دقيقة. تبقّى ": "you can mine once per minute. Time left: ",
            "لا توجد جولة ألماس نادر مفتوحة الآن. انتظر الإعلان القادم بعد 10 دقائق.": "No rare diamond round is open. Wait for the next announcement in 10 minutes.",
            "🏏 إعداد مباراة الكريكيت": "🏏 Cricket match setup",
            "🏏 إعداد مباراة ضد البوت": "🏏 Set up a match against the bot",
            "اختر عدد اللاعبين داخل هذه الغرفة فقط:": "Choose the player count for this room only:",
            "اختر عدد اللاعبين:": "Choose the number of players:",
            "اختر عدد اللاعبين في كل غرفة من 1 إلى 4.": "Choose 1–4 players per room.",
            "بعد اختيار العدد، يرسل كل لاعب Join من هذه الغرفة فقط.": "After choosing, each player types Join in this room only.",
            "Choose عدد Playerين:": "Choose the number of players:",
            "الخصم هو بوت ": "The opponent is the bot ",
            "أرسل Join من كل لاعب؛ بعد اكتمال العدد تبدأ المباراة ضد البوت.": "Each player types Join; the match against the bot starts when the roster is full.",
            "أرسل الرقم فقط (العدد لكل غرفة). بعد اكتمال الفريق الأول، ترسل الغرفة الثانية Join.": "Send only the number (players per room). Once the first team is full, the second room types Join.",
            "بعد اكتمال الفريق الأول، ترسل الغرفة الثانية Join.": "After the first team is full, the second room types Join.",
            "عند تعدد قوائم الانتظار، يُقرن Join بأقدم فريق أول مكتمل.": "If several lobbies are waiting, Join is paired with the oldest full first team.",
            "لا يمكن إكمال الفريقين من الغرفة نفسها.": "Both teams cannot be completed from the same room.",
            "اختر دورك: 1 للهجوم أو 2 للدفاع؛ وال": "Choose your role: 1 to bat or 2 to bowl; the ",
            "يختار: 1 هجوم أو 2 دفاع.": "chooses: 1 to bat or 2 to bowl.",
            "أرسل 0 إلى 6": "Send a number from 0 to 6",
            "دورك يا @": "Your turn, @",
            "🏏 الهجوم": "🏏 Batting",
            "🛡️ الدفاع": "🛡️ Bowling",
            "🏏 انتهت مباراة الكريكيت": "🏏 Cricket match ended",
            "🏏 بدأت لعبة الكريكيت في غرفة ": "🏏 Cricket started in room ",
            "🏏 فُتحت مباراة الكريكيت في ": "🏏 Cricket match opened in ",
            "🏏 بدأت مباراة الكريكيت": "🏏 Cricket match started",
            "👥 عدد الفريق:": "👥 Team size:",
            "🔗 للانضمام أرسل Join.": "🔗 Type Join to participate.",
            "✅ اكتمل الفريقان:": "✅ Both teams are full:",
            "👥 المطلوب ": "👥 Required: ",
            " لاعبين في كل غرفة.": " players per room.",
            "الفريق الأول يختار: 1 هجوم أو 2 دفاع.": "The first team chooses: 1 to bat or 2 to bowl.",
            "الفريق الثاني يُحدد تلقائيًا.": "The second team's role is assigned automatically.",
            "لا توجد مباراة مكتملة تنتظر الانضمام الآن.": "No full match is waiting for players right now.",
            "لا توجد قائمة لاعبين مفتوحة لهذه المباراة الآن.": "There is no open player lobby for this match.",
            "انتظر اختيار عدد اللاعبين أولاً.": "Wait for the player count to be selected first.",
            "لا يمكن للحساب نفسه المشاركة في مباراتين كركيت متزامنتين.": "The same account cannot join two simultaneous Cricket matches.",
            "⏳ هذه الغرفة مشاركة في مباراة مفتوحة بالفعل؛ أكملها أو أوقفها قبل بدء مباراة أخرى.": "⏳ This room is already in an open match. Finish or stop it before starting another.",
            "✅ انضم @": "✅ Joined @",
            "مسجل بالفعل في المباراة.": "is already registered for this match.",
            "اكتمل عدد اللاعبين (": "The player limit is reached (",
            "اكتمل الفريق الأول ": "The first team needs ",
            " لاعبين أولاً.": " players first.",
            "أرسل Join من لاعبي هذه الغرفة.": "Players in this room should type Join.",
            "أرسل Join من لاعبي الغرفة الأولى.": "Players in the first room should type Join.",
            "⏳ بانتظار اكتمال لاعبي الغرفة الأخرى.": "⏳ Waiting for the other room's players to finish joining.",
            "⏳ بانتظار انضمام الفريق الثاني.": "⏳ Waiting for the second team to join.",
            "⏳ بدأت المباراة بالفعل.": "⏳ The match has already started.",
            "📭 لا توجد مباراة.": "📭 There is no match.",
            "📭 اللعبة لا تنتظر اختيار الهجوم أو الدفاع.": "📭 The game is not waiting for a batting/bowling choice.",
            "❌ اختر 0 إلى 6.": "❌ Choose a number from 0 to 6.",
            "🏆 انتهت مباراة الكريكيت": "🏆 Cricket match ended",
            "النتيجة: ": "Result: ",
            "🏁 الشوط الأول انتهى": "🏁 First innings ended",
            "🎯 الهدف: ": "🎯 Target: ",
            "الكرة ": "Ball ",
            " لهذا الشوط": " this innings",
            "نقطة • الكرة": "points • ball",
            "تم تسجيل ضربة @": "Shot recorded for @",
            "🦆 بطّة": "🦆 Duck",
            "🔥 هاتريك — @": "🔥 Hat trick — @",
            "🔄 استؤنفت مباراة الكريكيت بعد إعادة تشغيل البوت.\n": "🔄 Cricket match resumed after bot restart.\n",
            "🔄 استؤنف إعداد مباراة الكريكيت. Choose عدد Playerين من 1 to 4.": "🔄 Cricket setup resumed. Choose the number of players from 1 to 4.",
            "🔄 استؤنفت مباراة الكريكيت. ترسل غرفة الإعداد 1 للهجوم or 2 للدفاع؛ ويُعيّن Turn: الغرفة الseconds تلقائيًا.": "🔄 Cricket match resumed. The setup room chooses 1 to bat or 2 to bowl; the second room's role is assigned automatically.",
            "✅ حُفظ اختيار الهجوم لهذه الكرة؛ انتظر رد فريق الدفاع.": "✅ Batting choice saved for this ball; wait for the bowling team's response.",
            "🏆 Ended مباراة الكريكيت": "🏆 Cricket match ended",
            "مباراة الكريكيت": "Cricket match",
            "⛔ أوقفت غرفة ": "⛔ Room ",
            " اللعبة؛ أُلغيت المباراة.": " match stopped; the match was cancelled.",
            "🏏 كركيت | مباراة ضد البوت — المطلوب ": "🏏 Cricket | vs bot — required ",
            "🏏 كركيت | ": "🏏 Cricket | ",
            " لاعب(ين) في هذه الغرفة.": " players in this room.",
            " لاعب(ين) في كل غرفة": " players per room",
            "لاعب(ين) في كل غرفة": "players per room",
            " — المطلوب ": " — required ",
            " لاعب(ين).": " players.",
            "لاعب(ين) في كل غرفة\n": "players per room\n",
            "، ": ", ",
            "🤖 انضم بوت ": "🤖 Bot ",
            " خصمًا لك. Choose دورك: 1 للهجوم or 2 للدفاع؛ والبوت يأخذ الTurn: الآخر تلقائيًا.": " joined as your opponent. Choose: 1 to bat or 2 to bowl; the bot takes the other role automatically.",
            " لاعبين أولاً.": " players first.",
            "🔗 انضمت غرفة ": "🔗 Room ",
            " to المباراة.": " joined the match.",
            "👥 المطلوب ": "👥 Required: ",
            "⏳ Turn: المهاجم @": "⏳ Batter's turn: @",
            "⏳ Turn: المدافع @": "⏳ Bowler's turn: @",
            "⛔ أُلغيت المباراة السابقة لبدء مباراة ضد البوت.": "⛔ The previous match was cancelled to start a bot match.",
            "⛔ أُلغيت المباراة المفتوحة من غرفة ": "⛔ The open match in room ",
            " لإعداد مباراة جديدة.": " was cancelled to set up a new match.",
            "👤 كل Playerين ينضمون من هذه الغرفة فقط بإرسال Join.": "👤 All players join from this room only by typing Join.",
            "🤖 عند اكتمال العدد تبدأ المباراة تلقائيًا ضد ": "🤖 When the roster is full, the match against the bot starts automatically: ",
            "🔗 بعد اكتمال الفريق الأول، ترسل الغرفة الseconds Join؛ وعند تعدد القوائم يُقرن بأقدم فريق مكتمل. or أكمل العدد هنا للعب ضد ": "🔗 After the first team is full, the second room types Join. If several lobbies are waiting, the oldest full team is paired first. Or fill this roster to play against ",
            " لاعبين لكل غرفة\n": " players per room\n",
            "🏏 اكتمل الفريقان: ": "🏏 Both teams are full: ",
            " players لكل غرفة": " players per room",
            " ضد ": " vs ",
            "🎯 الفريق الأول chooses: 1 to bat or 2 to bowl.": "🎯 The first team chooses: 1 to bat or 2 to bowl.",
            " لاعب في الغرفة.\n": " players in the room.\n",
            "🔄 استؤنفت قائمة الكريكيت؛ المطلوب ": "🔄 Cricket lobby resumed; required ",
            " لاعب(ين) في كل غرفة. تنضم الغرفة الثانية بإرسال Join؛ وعند تعدد القوائم يُقرن الطلب بأقدم فريق مكتمل.": " players per room. The second room types Join; if several lobbies are waiting, the oldest full team is paired first.",
            "🏏 تم الانضمام to ": "🏏 Joined ",
            " يا @": " — @",
            "🛡️ استؤنفت الكرة؛ Your turn, @": "🛡️ Ball resumed; your turn, @",
            ". أرسل 0 إلى 6": ". Send a number from 0 to 6",
            "الفريق الأول": "the first team",
            "الفريق الثاني": "the second team",
            "الغرفة الseconds": "the second room",
            "الفريق الseconds": "the second team",
            "الseconds": "the second",
            "المدافع": "bowler",
            "المهاجم": "batter",
            "دورك": "your turn",
            "الهجوم": "batting",
            "الدفاع": "bowling",
            "الفريق البشري": "human team",
            "تعادل": "tie",
            "الفريق": "team",
            " لاعبين ": " players ",
            "عدد اللاعبين": "number of players",
            "يا @": " — @",
            "خصمًا لك": "as your opponent",
            "للماستر المحدد في متغيرات السيرفر": "for the configured server master",
            "🏏 إعداد مباراة الكركيت": "🏏 Cricket match setup",
            "Choose عدد Playerين داخل هذه الغرفة فقط:": "Choose the player count for this room only:",
            "1️⃣ لاعب واحد\n2️⃣ لاعبان\n3️⃣ ثلاثة players\n4️⃣ أربعة players": "1️⃣ One player\n2️⃣ Two players\n3️⃣ Three players\n4️⃣ Four players",
            "Send the number only (العدد لكل غرفة).": "Send only the number (players per room).",
            "📢 سيعلن البوت اسم الغرف وعدد اللاعبين وأسماءهم عند اكتمال الفريقين.": "📢 The bot will announce the room names, player count and player names when both teams are full.",
            "❌ عدد Playerين في كل غرفة يجب أن يكون من 1 to 4.": "❌ Each room must have 1–4 players.",
            "❌ اكتب عدد Playerين في كل غرفة من 1 to 4.": "❌ Enter 1–4 players per room.",
            "❌ Could not تحديد اسم Player؛ أرسل Join من حسابك داخل الغرفة.": "❌ Could not identify the player; type Join from your account in the room.",
            "❌ Could not تحديد اسم Player.": "❌ Could not identify the player.",
            "📭 لا توجد لعبة تنتظر اختيار عدد Playerين.": "📭 No game is waiting for a player count.",
            "🔒 اختيار عدد Playerين متاح في غرفة بدء اللعبة فقط.": "🔒 Player count can only be selected in the game-start room.",
            "⏳ انتظر اختيار عدد Playerين أولاً.": "⏳ Wait for the player count to be selected first.",
            "⛔ اكتمل عدد Playerين (": "⛔ Player limit reached (",
            "⛔ اللعبة غير مفعّلة؛ فعّل اللعبة أولاً (فعّلها) من خاص الماستر: تشغيل لعبه الكريكيت.": "⛔ Cricket is disabled. Ask the master to enable it in private chat.",
            "🤖 اللعب against the bot متاح عند اختيار لاعب واحد لكل فريق فقط.": "🤖 Playing against the bot is available only with one player per team.",
            "⛔ خصم البوت متاح في غرفة بدء المباراة قبل انضمام غرفة أخرى.": "⛔ The bot opponent is available only in the match-start room before another room joins.",
            "⛔ يوجد لاعب مسجل بالفعل؛ أرسل Join بحسابه or انتظر الغرفة الأخرى.": "⛔ A player is already registered. Type Join from that account or wait for the other room.",
            "⛔ هذه الغرفة ليست في المباراة.": "⛔ This room is not in the match.",
            "⛔ أرسل Join أولاً.": "⛔ Type Join first.",
            "⛔ اكتملت غرفتا المباراة.": "⛔ Both match rooms are full.",
            "🤖 بوت ": "🤖 Bot ",
            "🎯 يبدأ batting: ": "🎯 Batting starts: ",
            ". لكل لاعب 6 كرات في كل دور.": ". Each player has 6 balls per innings.",
            "لاعبون قيد الانضمام": "Players joining",
            " لاعب(ين) في كل غرفة).\nتختار كل غرفة دورها: 1 للهجوم أو 2 للدفاع. يجب أن يكون هناك فريق من كل نوع.": " players per room).\nEach room chooses a role: 1 to bat or 2 to bowl. Both roles must be represented.",
            "👥 بعد اكتمال the first team, أرسل لاعبو the second room Join. عند تعدد قوائم الانتظار, يُختار أقدم فريق مكتمل.": "👥 Once the first team is full, players in the second room type Join. If several lobbies are waiting, the oldest full team is chosen.",
            " as your opponent. Choose your turn: 1 للهجوم or 2 للدفاع؛ والبوت يأخذ الTurn: الآخر تلقائيًا.": " as your opponent. Choose: 1 to bat or 2 to bowl; the bot takes the other role automatically.",
            " players in the room.\n🔗 بعد اكتمال the first team, ترسل the second room Join؛ وعند تعدد القوائم يُقرن بأقدم فريق مكتمل. or أكمل العدد هنا للعب vs ": " players in the room.\n🔗 After the first team is full, the second room types Join. If several lobbies are waiting, the oldest full team is paired first. Or fill this roster to play against ",
            "🔄 استؤنفت مباراة الكريكيت بعد إعادة تشغيل البوت.\n": "🔄 Cricket match resumed after the bot restarted.\n",
            "🔄 استؤنف إعداد مباراة الكريكيت. Choose عدد Playerين من 1 to 4.": "🔄 Cricket setup resumed. Choose 1–4 players.",
            "🔄 استؤنفت مباراة الكريكيت. ترسل غرفة الإعداد 1 للهجوم or 2 للدفاع؛ ويُعيّن Turn: the second room تلقائيًا.": "🔄 Cricket match resumed. The setup room chooses 1 to bat or 2 to bowl; the second room's role is assigned automatically.",
            "🔄 استؤنفت قائمة الكريكيت؛ المطلوب ": "🔄 Cricket lobby resumed; required ",
            " players per room. تنضم the second room بإرسال Join؛ وعند تعدد القوائم يُقرن الطلب بأقدم فريق مكتمل.": " players per room. The second room types Join; the oldest full team is paired first when multiple lobbies are waiting.",
            " — أرسل 0 to 6.\n🛡️ بعده Turn: @": " — send 0 to 6.\n🛡️ Then the turn passes to @",
            "❌ اسم الغرفة غير صالح.": "❌ Invalid room name.",
            "📭 لا توجد قائمة players مفتوحة لهذه المباراة الآن.": "📭 No player lobby is open for this match right now.",
            "⛔ يوجد لاعب مسجل بالفعل؛ أرسل Join بحسابه or انتظر الغرفة الأخرى.": "⛔ A player is already registered. Type Join from that account or wait for the other room.",
            " players per room).\nتختار كل غرفة دورها: 1 للهجوم or 2 للدفاع. يجب أن يكون هناك فريق من كل نوع.": " players per room).\nEach room chooses its role: 1 to bat or 2 to bowl. Both roles must be represented.",
            "بعد اكتمالها, ترسل the second room Join.": "once it is full, the second room types Join.",
            " عند تعدد قوائم الانتظار, يُقرن Join بأقدم فريق أول مكتمل.": " When multiple lobbies are waiting, Join is paired with the oldest full first team.",
            "🏏 فُتحت مباراة الكركيت في ": "🏏 Cricket match opened in ",
            "as your opponent. Choose your turn: 1 للهجوم or 2 للدفاع؛ والبوت يأخذ الTurn: الآخر تلقائيًا.": "as your opponent. Choose: 1 to bat or 2 to bowl; the bot takes the other role automatically.",
            " players in the room.\n🔗 بعد اكتمال the first team, ترسل the second room Join؛ وعند تعدد القوائم يُقرن بأقدم فريق مكتمل. or أكمل العدد هنا للعب vs ": " players in the room.\n🔗 After the first team is full, the second room types Join. The oldest full team is paired first. Or fill this roster to play against ",
            " لاعب في هذه الغرفة.\n⏳ Waiting for the other room's players to finish joining.": " players in this room.\n⏳ Waiting for the other room's players to finish joining.",
            "🔄 استؤنفت": "🔄 Resumed",
            "🔄 استؤنف": "🔄 Resumed",
            "مباراة الكريكيت": "Cricket match",
            "مباراة الكركيت": "Cricket match",
            "إعداد مباراة": "match setup",
            "الكريكيت": "Cricket",
            "الكركيت": "Cricket",
            "إعادة تشغيل البوت": "bot restart",
            "من 1 إلى 4": "from 1 to 4",
            "عدد Playerين": "player count",
            "ترسل غرفة الإعداد": "the setup room sends",
            "1 للهجوم": "1 to bat",
            "2 للدفاع": "2 to bowl",
            "ويُعيّن Turn: ": "and the role is assigned to ",
            "تلقائيًا": "automatically",
            "انتظر": "wait",
            "اسم الغرفة غير صالح": "invalid room name",
            "أرسل Join بحسابه": "type Join from that account",
            "انتظر الغرفة الأخرى": "wait for the other room",
            "تختار كل غرفة دورها": "each room chooses its role",
            "يجب أن يكون هناك فريق من كل نوع": "both roles must be represented",
            "بعد اكتمالها": "once it is full",
            "ترسل the second room Join": "the second room types Join",
            "وعند تعدد القوائم يُقرن بأقدم فريق مكتمل": "and the oldest full team is paired first when several lobbies are waiting",
            "لاعب في هذه الغرفة": "players in this room",
            "انتظار": "waiting",
            "الغرفة الأخرى": "the other room",
            ") في هذه الغرفة.": ") players in this room.",
            "بعد اكتمال the first team": "After the first team is full",
            "؛ وعند تعدد القوائم يُقرن بأقدم فريق مكتمل.": "; the oldest full team is paired first when several lobbies are waiting.",
            "؛ والبوت يأخذ الTurn: الآخر automatically.": "; the bot takes the other role automatically.",
            "⛔ تم إيقاف لعبة Cricket وإلغاء المباراة المفتوحة.": "⛔ Cricket was disabled and the open match was cancelled.",
            "✅ تم تشغيل ملفات لعبة Cricket. أرسل .cr 1 داخل الغرفة لفتح إعداد المباراة للأعضاء.": "✅ Cricket is enabled. Type .cr 1 in the room to open match setup for members.",
            "✅ ملفات لعبة Cricket مفعّلة؛ المباراة المفتوحة مستمرة دون تغيير.": "✅ Cricket is enabled; the open match continues unchanged.",
            "🔒 لعبة Cricket متاحة للأعضاء الموثقين فقط.": "🔒 Cricket is available to verified members only.",
            "📌 افتح مباراة cricket 1..4 داخل الغرفة, وليس في الخاص.": "📌 Start cricket 1–4 inside a room, not in private chat.",
            "🔄 Resumed Cricket match بعد bot restart.\n": "🔄 Cricket match resumed after bot restart.\n",
            "🔄 Resumed إعداد Cricket match. Choose player count من 1 to 4.": "🔄 Cricket setup resumed. Choose 1–4 players.",
            "🔄 Resumed Cricket match. the setup room sends 1 to bat or 2 to bowl؛ and the role is assigned to the second room automatically.": "🔄 Cricket match resumed. The setup room chooses to bat or bowl; the second room's role is assigned automatically.",
            "🔄 Resumed قائمة Cricket؛ المطلوب ": "🔄 Cricket lobby resumed; required ",
            " players per room. تنضم the second room بإرسال Join؛ وعند تعدد القوائم يُقرن الطلب بأقدم فريق مكتمل.": " players per room. The second room types Join; the oldest full team is paired first when several lobbies are waiting.",
            "⛔ يوجد لاعب مسجل بالفعل؛ type Join from that account or wait the other room.": "⛔ A player is already registered. Type Join from that account or wait for the other room.",
            "؛ and the oldest full team is paired first when several lobbies are waiting.": "; the oldest full team is paired first when several lobbies are waiting.",
            " or أكمل العدد هنا للعب vs ": " or complete this roster to play against ",
            "🔄 Resumed Cricket match. the setup room sends 1 to bat or 2 to bowl؛ and the role is assigned to the second room automatically.": "🔄 Cricket match resumed. The setup room chooses 1 to bat or 2 to bowl; the second room's role is assigned automatically.",
            "cricket is enabled": "Cricket is enabled",
            "diamond نادرة RED:": "rare RED diamond:",
            "diamond Rare RED:": "Rare RED diamond:",
            "للْقطعة": "per item", "للقطعة": "per item",
            "رمز أو اسم أي كنز العدد — مثال: ": "<item name or icon> <quantity> — example: ",
            "نادرة RED:": "rare RED diamond:",
            " أو ": " or ",
        }
        # Longer Arabic item names must be replaced before shorter fragments.
        for ar, en in mining_and_cricket_replacements.items():
            value = value.replace(ar, en)
        final_cricket_fixes = {
            "⛏️ متجر الكنوز — الأسعار بالpoints": "⛏️ Treasure shop — prices in points",
            "team الأول": "the first team",
            "team الثاني": "the second team",
            "⛔ يوجد لاعب مسجل بالفعل؛ type Join from that account or wait the other room.":
                "⛔ A player is already registered. Type Join from that account or wait for the other room.",
            "لاعب في الغرفة.\n🔗 After the first team is full, the second room types Join؛ the oldest full team is paired first when several lobbies are waiting. or أكمل العدد هنا للعب vs ":
                "Players in the room.\n🔗 After the first team is full, the second room types Join. The oldest full team is paired first when several lobbies are waiting. Or complete this roster to play against ",
            "لاعب في الغرفة.\n🔗 After the first team is full, the second room types Join؛ and the oldest full team is paired first when several lobbies are waiting. or أكمل العدد هنا للعب vs ":
                "Players in the room.\n🔗 After the first team is full, the second room types Join. The oldest full team is paired first when several lobbies are waiting. Or complete this roster to play against ",
            "لاعب في الغرفة.\n🔗 After the first team is full, the second room types Join; the oldest full team is paired first when several lobbies are waiting. or أكمل العدد هنا للعب vs ":
                "Players in the room.\n🔗 After the first team is full, the second room types Join. The oldest full team is paired first when several lobbies are waiting. Or complete this roster to play against ",
            "🔄 Resumed Cricket match. the setup room sends 1 to bat or 2 to bowl؛ and the role is assigned to the second room automatically.":
                "🔄 Cricket match resumed. The setup room chooses 1 to bat or 2 to bowl; the second room's role is assigned automatically.",
        }
        for old, new in final_cricket_fixes.items():
            value = value.replace(old, new)
        auction_help_fixes = {
            "🔨 المزاد العالمي: الماستر في الخاص يرسل شغل لعبة مزاد / أوقف لعبة مزاد.":
                "🔨 World auction: the master enables/disables it in private with “auction on” / “auction off”.",
            "افتح مزاد — يختار البوت سلعة Rare ويرسل صورتها (≤50 KB).":
                "Open auction — the bot selects a rare item and sends its image (≤50 KB).",
            "مزايده — تسجيل؛ عند 3 مشاركين يبدأه الفاتح, وعند 5 يبدأ تلقائياً.":
                "Join auction — register; at 3 players the host may start, or it starts automatically at 5.",
            "بعد البدء: المزايدة بالTurn: لمدة 60 seconds. عند your turn اكتب «زايد <المبلغ>»؛ أول مزايدة لا تقل عن السعر الابتدائي, وكل مزايدة لاحقة أعلى من السعر الحالي بـ10,000,000 points على الأقل, or اكتب «تمرير». يتجاهل البوت أوامر المزايدة خارج الTurn: بصمت.":
                "After bidding starts, turns last 60 seconds overall, with each turn advancing automatically after 10 seconds without a bid. On your turn, enter “bid <amount>” or “pass”. The first bid must meet the starting price, and each later bid must exceed the current high bid by at least 10,000,000 points. Amounts accept shorthand such as 10m (10 million), 10b or 1b (billions), or combined forms like 10m10b. Out-of-turn bid commands are ignored silently.",
            "أثناء الجولة, مزايده تعني أن المزاد شغال؛ wait انتهاءه. فاتح الجولة or الماستر يستطيع إقفاله بأمر «اقفل المزاد»." :
                "While a round is running, registration is closed; wait until it ends. The host or master may end it with “close auction”.",
            "sv — عرض مقتنيات المزاد؛ red auction <الرمز> — الاسترداد بالحظ بنسبة الربح/الخسارة المضبوطة.":
                "sv — view auction collectibles; redeem: 1% chance of profit, 1% chance of loss, and 98% no change.",
        }
        for old, new in auction_help_fixes.items():
            value = value.replace(old, new)
        auction_help_final_fixes = {
            "بعد الإغلاق: «زايد/مزايده» يرد بأن المزاد مغلق وwait جولة أخرى.":
                "After the auction closes, “bid/join auction” gets a local reply: the auction is closed; wait for another round.",
            "sv — عرض مقتنيات المزاد؛ red auction <الرمز> — الاسترداد بالحظ (50/50): أصلية بربح 0.75% or مغشوشة بخسارة 1.25%.":
                "sv — view auction collectibles; redeem: 1% chance of profit, 1% chance of loss, and 98% no change.",
        }
        for old, new in auction_help_final_fixes.items():
            value = value.replace(old, new)
        value = value.replace(
            "بعد البدء: المزايدة بالدور لمدة 60 ثانية.",
            "بعد البدء: تستمر المزايدة 60 ثانية، ولكل لاعب 10 ثوانٍ لإرسال المبلغ قبل انتقال الدور تلقائيًا.",
        )
        value = value.replace(
            "الاسترداد بالحظ: 1% ربح، 1% خسارة، و98% دون ربح أو خسارة.",
            "الاسترداد بالحظ: 1% ربح، 1% خسارة، و98% دون ربح أو خسارة.",
        )
        return value
