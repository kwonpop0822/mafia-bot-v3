import asyncio
import logging
import os
import random
import string
from pathlib import Path
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from mafia_v3 import MafiaStore, display_name, register_v3_handlers


# ============================================================
# 0. 기본 설정
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
DEV_PASSWORD = os.getenv("MAFIA_DEV_PASSWORD", "20150822")

MAX_PLAYERS = 16


# ============================================================
# 1. 전역 데이터
# ============================================================

rooms = {}
store = MafiaStore(Path(os.getenv("MAFIA_DATA_DIR", "data")) / "mafia_v3.sqlite3")

dev_forced_roles = {}

user_link_group = {}
linked_accounts = {}

_bot_id_counter = -100000


def generate_bot_id():
    global _bot_id_counter
    bot_id = _bot_id_counter
    _bot_id_counter -= 1
    return bot_id


def generate_room_code():
    """충돌하지 않는 5자리 공개 방 코드를 생성한다."""
    while True:
        code = "".join(
            random.choices(
                string.ascii_uppercase + string.digits,
                k=5
            )
        )
        if code not in rooms:
            return code


def add_ai_bot_to_room(room):
    """방에 AI 참가자 한 명을 추가하고 생성된 플레이어를 반환한다."""
    if len(room.players) >= MAX_PLAYERS:
        return None
    bot_id = generate_bot_id()
    bot_name = f"Bot_{abs(bot_id) % 1000:03d}"
    if not room.add_player(bot_id, bot_name, is_bot=True):
        return None
    return room.players[bot_id]


# ============================================================
# 2. 봇 성격
# ============================================================

BOT_PERSONALITIES = [
    {
        "type": "공격형",
        "intro": [
            "일단 한 명 잡고 보죠.",
            "말 돌리지 말고 범인부터 찾읍시다.",
            "지금 분위기 이상한 사람 있음."
        ],
        "style": "강하게 몰아붙인다."
    },
    {
        "type": "신중형",
        "intro": [
            "아직 확정하기는 이른 것 같습니다.",
            "조금 더 지켜봐야 할 것 같아요.",
            "투표 기록부터 보는 게 맞습니다."
        ],
        "style": "쉽게 확신하지 않는다."
    },
    {
        "type": "음모론자",
        "intro": [
            "이거 누가 뒤에서 판 짜고 있음.",
            "지금 상황 너무 이상한데?",
            "뭔가 숨겨진 게 있는 것 같은데."
        ],
        "style": "모든 것을 의심한다."
    },
    {
        "type": "허세형",
        "intro": [
            "제가 보기엔 거의 답 나왔습니다.",
            "이건 제가 감으로 압니다.",
            "지금부터 제가 캐리합니다."
        ],
        "style": "자신감이 매우 높다."
    },
    {
        "type": "따라쟁이",
        "intro": [
            "저도 일단 그쪽 의견에 동의합니다.",
            "저도 같은 생각입니다.",
            "방금 말이 꽤 설득력 있네요."
        ],
        "style": "다른 플레이어 의견에 영향을 받는다."
    },
]


def create_bot_personality():
    p = random.choice(BOT_PERSONALITIES)

    return {
        "type": p["type"],
        "intro": random.choice(p["intro"]),
        "style": p["style"],
        "trust": {},
        "anger": {},
        "confidence": random.randint(30, 80),
    }


# ============================================================
# 3. 봇 대화 엔진
# ============================================================

class DynamicChatEngine:

    @staticmethod
    def attack(target_name, prob, personality):
        openings = [
            f"[{target_name}] 계속 행동이 이상함.",
            f"제가 보기엔 [{target_name}] 쪽이 제일 수상합니다.",
            f"아까부터 [{target_name}] 행동이 너무 이상한데요.",
            f"지금 [{target_name}] 말이 앞뒤가 안 맞음.",
            f"[{target_name}] 투표 흐름을 보면 좀 이상합니다.",
        ]

        endings = [
            "일단 여기부터 보는 게 맞다고 봅니다.",
            "저는 여기 한 표 던집니다.",
            "괜히 다른 사람 찍었다가 후회할 듯.",
            "해명 제대로 해보세요.",
            "이거 놓치면 시민 쪽이 힘들어집니다.",
        ]

        return (
            f"{random.choice(openings)} "
            f"마피아 의심도 {prob}% 정도로 봅니다. "
            f"{random.choice(endings)}"
        )

    @staticmethod
    def defense(target_name, personality):
        starts = [
            "잠깐만요 ㅋㅋㅋ",
            "아니 이건 진짜 억울한데요?",
            "지금 상황 이상합니다.",
            "왜 갑자기 제가 타겟임?",
            "이건 너무 쉽게 몰아가는 거 아닌가요?",
        ]

        counters = [
            f"오히려 [{target_name}] 쪽부터 조사해야 합니다.",
            f"저를 몰아가는 흐름 자체가 이상합니다.",
            "저 죽으면 시민 쪽 진짜 힘들어집니다.",
            "지금 저를 잡는 게 마피아한테 제일 좋은 그림입니다.",
        ]

        return f"{random.choice(starts)} {random.choice(counters)}"

    @staticmethod
    def ghost(name):
        lines = [
            f"👻 [{name}] : 아 죽으니까 진짜 할 거 없네 ㅋㅋ",
            f"👻 [{name}] : 살아있는 사람들아 정신 차려라...",
            f"👻 [{name}] : 와 저걸 믿네 ㅋㅋㅋㅋ",
            f"👻 [{name}] : 저승에서 팝콘 뜯는 중.",
            f"👻 [{name}] : 죽고 나니까 모든 게 보인다...",
        ]

        return random.choice(lines)

    @staticmethod
    def reaction_to_death(name, role):
        reactions = [
            f"헐 [{name}] 죽었네.",
            f"[{name}]이 진짜 죽었다고?",
            f"이거 판 완전히 바뀌었는데.",
            f"잠깐만, [{name}]이 죽었다는 건...",
            f"와 이거 예상 못했음.",
        ]

        if role == "마피아":
            reactions.append(
                f"잠깐. [{name}] 마피아였으면 이제 계산이 달라지는데?"
            )

        return random.choice(reactions)


# ============================================================
# 4. AI 엔진
# ============================================================

class MafiaAI:

    @staticmethod
    def calculate_suspicion(bot_id, room):

        alive = room.get_alive_players()

        result = {}

        for uid, player in alive.items():

            if uid == bot_id:
                continue

            score = 30.0

            # 공개 직업
            if uid in room.public_known_roles:

                role = room.public_known_roles[uid]

                if role == "마피아":
                    result[uid] = 100
                    continue

                result[uid] = 0
                continue

            # 자기 마피아 선언
            if uid in room.self_proclaimed_mafias:
                score += 60

            # 광고자
            if player["role"] == "광고자" and not room.spammer_gave_up:
                score += 25

            # 최근 투표
            previous_target = room.previous_votes.get(uid)

            if previous_target is not None:

                target = room.players.get(previous_target)

                if target:

                    if target["role"] in ["경찰", "의사"]:
                        score += 12

            # 랜덤 사건
            score += room.event_suspicion.get(uid, 0)

            # 봇 개인 기억
            personality = player.get("personality")

            if personality:

                trust = personality["trust"].get(uid, 0)
                anger = personality["anger"].get(uid, 0)

                score -= trust
                score += anger

            score += random.uniform(-8, 8)

            result[uid] = max(1, min(99, score))

        return result

    @staticmethod
    def choose_target(bot_id, role, room, candidates):

        if not candidates:
            return None

        scores = MafiaAI.calculate_suspicion(
            bot_id,
            room
        )

        personality = room.players[bot_id].get("personality")

        weighted = []

        for uid in candidates:

            score = scores.get(uid, 30)

            # 공격형
            if personality and personality["type"] == "공격형":
                score += random.uniform(0, 20)

            # 신중형
            elif personality and personality["type"] == "신중형":
                score += random.uniform(-5, 8)

            # 음모론자
            elif personality and personality["type"] == "음모론자":
                score += random.uniform(5, 25)

            weighted.append((score, uid))

        weighted.sort(reverse=True)

        # 70%는 최고 의심 대상
        if random.random() < 0.7:
            return weighted[0][1]

        return random.choice(candidates)

    @staticmethod
    def speech(bot_id, room):

        player = room.players[bot_id]
        role = player["role"]

        personality = player.get("personality")

        alive = room.get_alive_players()

        candidates = [
            uid for uid in alive
            if uid != bot_id
        ]

        suspicion = MafiaAI.calculate_suspicion(
            bot_id,
            room
        )

        if not candidates:
            return "..."

        target = max(
            candidates,
            key=lambda uid: suspicion.get(uid, 0)
        )

        target_name = room.players[target]["name"]

        probability = int(
            suspicion.get(target, 30)
        )

        # 광고자
        if role == "광고자":
            ads = [
                f"여러분 잠깐만요. 이거 보고 투표합시다 👉 {room.ad_link}",
                f"마피아보다 중요한 게 있습니다. 링크 클릭 ㄱㄱ 👉 {room.ad_link}",
                f"투표 전에 광고 한 번만 봐주시면 안 됩니까 ㅠㅠ 👉 {room.ad_link}",
            ]

            return random.choice(ads)

        # 마피아
        if role == "마피아":

            # 시민에게 의심 돌리기
            return DynamicChatEngine.attack(
                target_name,
                probability,
                personality
            )

        # 시민 계열
        if room.state == "DEFENSE":

            return DynamicChatEngine.defense(
                target_name,
                personality
            )

        # 공개 마피아
        for uid, known_role in room.public_known_roles.items():

            if known_role == "마피아" and uid in alive:

                name = room.players[uid]["name"]

                return random.choice([
                    f"[{name}] 마피아 확정이잖아요. 왜 고민함?",
                    f"기자 결과 떴으면 [{name}]부터 잡죠.",
                    f"[{name}] 살려두면 시민이 손해입니다.",
                ])

        return DynamicChatEngine.attack(
            target_name,
            probability,
            personality
        )


# ============================================================
# 5. MafiaRoom
# ============================================================

class MafiaRoom:

    def __init__(self, room_code, host_id, password=None):

        self.room_code = room_code
        self.host_id = host_id
        self.password = password

        self.state = "WAITING"

        self.players = {}

        self.target_mafia_count = 1

        self.day = 0

        # 광고
        self.ad_link = "유튜브 [구독과 좋아요] 부탁드립니다!!"

        self.spammer_protected = False
        self.spammer_gave_up = False
        self.silenced_target = None

        # v3 로비 설정 (게임 시작 전 방장만 변경 가능)
        self.settings = {
            "random_events": True,
        }

        # 공개 정보
        self.public_known_roles = {}
        self.self_proclaimed_mafias = set()

        # 밤 행동
        self.mafia_choices = {}
        self.doctor_target = None
        self.police_target = None
        self.reporter_target = None
        self.gangster_target = None

        # 유령
        self.ghost_predictions = {}

        # 사망
        self.last_dead = None
        self.last_executed = None
        self.medium_memory = None

        # 투표
        self.votes = {}
        self.final_votes = {}

        self.defense_target = None

        # AI
        self.previous_votes = {}
        self.event_suspicion = {}

        # 랜덤 사건
        self.current_event = None

        # 전적
        self.stats = defaultdict(int)

        # 밤에 아직 행동하지 않은 플레이어
        self.night_pending = set()

        # 게임 로그
        self.highlights = []

    # --------------------------------------------------------

    def add_player(self, user_id, name, is_bot=False):

        if len(self.players) >= MAX_PLAYERS:
            return False

        if user_id in self.players:
            return False

        self.players[user_id] = {
            "name": display_name(name),
            "role": None,
            "alive": True,
            "is_bot": is_bot,
            "personality": (
                create_bot_personality()
                if is_bot else None
            ),
        }

        return True

    # --------------------------------------------------------

    def get_alive_players(self):

        return {
            uid: player
            for uid, player in self.players.items()
            if player["alive"]
        }

    # --------------------------------------------------------

    def get_ghost_players(self):

        return {
            uid: player
            for uid, player in self.players.items()
            if not player["alive"]
        }

    # --------------------------------------------------------

    def assign_roles(self):

        global dev_forced_roles

        player_ids = list(self.players.keys())

        total = len(player_ids)

        assigned = set()

        # 강제 직업
        for pid in player_ids:

            if pid in dev_forced_roles:

                self.players[pid]["role"] = dev_forced_roles[pid]

                assigned.add(pid)

                del dev_forced_roles[pid]

        remaining = [
            pid
            for pid in player_ids
            if pid not in assigned
        ]

        if not remaining:
            return

        mafia_count = min(
            self.target_mafia_count,
            max(1, total // 3)
        )

        current_mafia = sum(
            1
            for pid in assigned
            if self.players[pid]["role"] == "마피아"
        )

        needed_mafia = max(
            0,
            mafia_count - current_mafia
        )

        pool = ["마피아"] * needed_mafia

        special_roles = [
            "경찰",
            "의사",
            "기자",
            "정치인",
            "건달",
            "영매",
            "테러리스트",
            "광고자",
        ]

        random.shuffle(special_roles)

        # 인원이 많을수록 특수직 증가
        special_count = min(
            len(special_roles),
            max(1, (total - mafia_count) // 3)
        )

        already_special = {
            self.players[pid]["role"]
            for pid in assigned
        }

        for role in special_roles:

            if role in already_special:
                continue

            if len(pool) >= len(remaining):
                break

            if len(pool) - needed_mafia < special_count:

                pool.append(role)

        while len(pool) < len(remaining):
            pool.append("시민")

        random.shuffle(pool)

        for pid, role in zip(remaining, pool):
            self.players[pid]["role"] = role

    # --------------------------------------------------------

    def alive_mafias(self):

        return [
            uid
            for uid, player in self.get_alive_players().items()
            if player["role"] == "마피아"
        ]

    # --------------------------------------------------------

    def choose_mafia_target(self):

        if not self.mafia_choices:
            return None

        counter = Counter(
            self.mafia_choices.values()
        )

        max_count = max(counter.values())

        targets = [
            target
            for target, count in counter.items()
            if count == max_count
        ]

        return random.choice(targets)

    # --------------------------------------------------------

    def reset_night(self):

        self.mafia_choices = {}

        self.doctor_target = None
        self.police_target = None
        self.reporter_target = None
        self.gangster_target = None

        self.ghost_predictions = {}

        self.night_pending = set()

    # --------------------------------------------------------

    def add_highlight(self, text):

        self.highlights.append(text)

        if len(self.highlights) > 15:
            self.highlights.pop(0)


# ============================================================
# 6. 메시지 전송
# ============================================================

async def send_safe(
    context,
    user_id,
    text,
    reply_markup=None,
    parse_mode="Markdown"
):

    try:

        return await context.bot.send_message(
            user_id,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )

    except Exception:

        try:

            return await context.bot.send_message(
                user_id,
                text,
                reply_markup=reply_markup
            )

        except Exception:

            return None


async def broadcast_all(
    context,
    room,
    text
):

    for uid, player in room.players.items():

        if not player["is_bot"]:

            await send_safe(
                context,
                uid,
                text
            )


async def broadcast_alive(
    context,
    room,
    text,
    sender_id=None
):

    for uid, player in room.get_alive_players().items():

        if (
            not player["is_bot"]
            and uid != sender_id
        ):

            await send_safe(
                context,
                uid,
                text
            )


async def broadcast_ghosts(
    context,
    room,
    text,
    sender_id=None
):

    for uid, player in room.get_ghost_players().items():

        if (
            not player["is_bot"]
            and uid != sender_id
        ):

            await send_safe(
                context,
                uid,
                text
            )


# ============================================================
# 7. 연동
# ============================================================

def auto_add_linked_accounts(
    room,
    initiator_id
):

    if initiator_id not in user_link_group:
        return

    group_code = user_link_group[initiator_id]

    members = linked_accounts.get(
        group_code,
        {}
    )

    for uid, name in members.items():

        if uid != initiator_id:

            room.add_player(
                uid,
                name,
                is_bot=False
            )


async def link_account_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    user_name = update.effective_user.first_name

    args = update.message.text.split()[1:]

    if not args:

        if user_id in user_link_group:

            code = user_link_group.pop(user_id)

            if code in linked_accounts:

                linked_accounts[code].pop(
                    user_id,
                    None
                )

            await update.message.reply_text(
                "🔗 **파티 연동 해제 완료!**",
                parse_mode="Markdown"
            )

        else:

            await update.message.reply_text(
                "❌ 사용법: `/연동 비밀코드`",
                parse_mode="Markdown"
            )

        return

    code = args[0]

    if user_id in user_link_group:

        old = user_link_group.pop(user_id)

        linked_accounts.get(
            old,
            {}
        ).pop(
            user_id,
            None
        )

    user_link_group[user_id] = code

    linked_accounts.setdefault(
        code,
        {}
    )[user_id] = user_name

    members = ", ".join(
        linked_accounts[code].values()
    )

    await update.message.reply_text(
        f"🔗 **연동 완료!**\n"
        f"코드: `{code}`\n"
        f"파티원: {members}",
        parse_mode="Markdown"
    )


# ============================================================
# 8. 시작 / 도움말
# ============================================================

async def start_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    name = update.effective_user.first_name

    text = (
        f"🔥 **어서와라, {name}!**\n\n"
        f"🎭 **마피아 봇 2.0**\n"
        f"최대 {MAX_PLAYERS}인 예능형 마피아!\n\n"
        f"📌 명령어\n"
        f"• `/fast` 빠른 게임\n"
        f"• `/create` 방 생성\n"
        f"• `/join 방코드` 입장\n"
        f"• `/add_bot 방코드` AI 추가\n"
        f"• `/start_game 방코드` 게임 시작\n"
        f"• `/set_mafia 방코드 숫자` 마피아 수 설정\n"
        f"• `/연동 코드` 계정 연동\n"
        f"• `/v3help` v3 운영 명령어\n\n"
        f"📊 `/profile`, `/rank`, `/history` 영구 전적\n"
        f"🎮 `/room`, `/rooms`, `/roles` 로비·직업 안내\n"
        f"🎲 매일 랜덤 사건 발생\n"
        f"🤖 AI 봇 성격 시스템\n"
        f"👻 유령 채팅\n"
        f"💣 테러리스트\n"
        f"📰 기자\n"
        f"👻 영매\n"
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown"
    )


# ============================================================
# 9. 방 생성
# ============================================================

async def create_room(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    password = (
        context.args[0]
        if context.args
        else None
    )

    code = generate_room_code()

    room = MafiaRoom(
        code,
        user.id,
        password
    )

    rooms[code] = room

    room.add_player(
        user.id,
        user.first_name
    )
    store.touch_user(user.id, user.first_name)

    auto_add_linked_accounts(
        room,
        user.id
    )
    store.save_room_snapshot(room)

    password_text = (
        f"🔒 비번 `{password}`"
        if password
        else "🔓 비번 없음"
    )

    await update.message.reply_text(
        f"🎮 **방 생성 완료!**\n\n"
        f"🔑 방 코드: `{code}`\n"
        f"{password_text}\n\n"
        f"입장: `/join {code}`",
        parse_mode="Markdown"
    )


async def join_room(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:
        return

    user = update.effective_user

    code = context.args[0].upper()

    room = rooms.get(code)

    if not room:

        await update.message.reply_text(
            "❌ 그런 방 없음."
        )

        return

    if room.state != "WAITING":

        await update.message.reply_text(
            "❌ 이미 게임 시작함."
        )

        return

    if len(room.players) >= MAX_PLAYERS:

        await update.message.reply_text(
            "❌ 방이 꽉 찼습니다."
        )

        return

    if room.password:

        if len(context.args) < 2:

            await update.message.reply_text(
                "🔒 비밀번호 필요."
            )

            return

        if context.args[1] != room.password:

            await update.message.reply_text(
                "❌ 비밀번호 틀림."
            )

            return

    if room.add_player(
        user.id,
        user.first_name
    ):

        store.touch_user(user.id, user.first_name)
        auto_add_linked_accounts(
            room,
            user.id
        )
        store.save_room_snapshot(room)

        await update.message.reply_text(
            f"✅ `{code}` 입장 완료!\n"
            f"현재 {len(room.players)}/{MAX_PLAYERS}명",
            parse_mode="Markdown"
        )


# ============================================================
# 10. 봇 추가
# ============================================================

async def add_bot(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:
        await update.message.reply_text(
            "사용법: `/add_bot 방코드`"
        )
        return

    code = context.args[0].upper()

    room = rooms.get(code)

    if not room:
        return

    if room.state != "WAITING":
        await update.message.reply_text("❌ AI 추가는 대기 로비에서만 가능합니다.")
        return

    if update.effective_user.id != room.host_id:
        await update.message.reply_text("❌ 방장만 AI를 추가할 수 있습니다.")
        return

    player = add_ai_bot_to_room(room)
    if player is None:
        await update.message.reply_text("❌ 방이 가득 찼습니다.")
        return

    personality = player["personality"]
    store.save_room_snapshot(room)

    await update.message.reply_text(
        f"🤖 **AI 참가!**\n"
        f"이름: `{player['name']}`\n"
        f"성격: **{personality['type']}**",
        parse_mode="Markdown"
    )


# ============================================================
# 11. 빠른 시작
# ============================================================

async def fast_start_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    buttons = [
        [
            InlineKeyboardButton(
                "🤖 4명",
                callback_data="fastbot_4"
            ),
            InlineKeyboardButton(
                "🤖 6명",
                callback_data="fastbot_6"
            ),
            InlineKeyboardButton(
                "🤖 8명",
                callback_data="fastbot_8"
            ),
        ],
        [
            InlineKeyboardButton(
                "🤖 10명",
                callback_data="fastbot_10"
            ),
            InlineKeyboardButton(
                "🤖 12명",
                callback_data="fastbot_12"
            ),
            InlineKeyboardButton(
                "🤖 15명",
                callback_data="fastbot_15"
            ),
        ],
    ]

    await update.message.reply_text(
        "⚡ **FAST GAME**\n\n"
        "AI 봇 수를 선택하세요!",
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
        parse_mode="Markdown"
    )


async def fast_bot_cb(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    count = int(
        query.data.split("_")[1]
    )

    max_mafia = min(
        4,
        max(
            1,
            (count + 1) // 3
        )
    )

    buttons = []

    for i in range(1, max_mafia + 1):

        buttons.append([
            InlineKeyboardButton(
                f"😈 마피아 {i}명",
                callback_data=f"fastmaf_{count}_{i}"
            )
        ])

    await query.edit_message_text(
        "⚡ **마피아 수 선택!**",
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
        parse_mode="Markdown"
    )


async def fast_mafia_cb(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    _, bot_count, mafia_count = (
        query.data.split("_")
    )

    user = query.from_user

    code = generate_room_code()

    room = MafiaRoom(
        code,
        user.id
    )

    rooms[code] = room

    room.target_mafia_count = int(
        mafia_count
    )

    room.add_player(
        user.id,
        user.first_name
    )
    store.touch_user(user.id, user.first_name)

    auto_add_linked_accounts(
        room,
        user.id
    )

    for _ in range(int(bot_count)):
        add_ai_bot_to_room(room)

    room.assign_roles()

    room.state = "SETTING_AD"
    store.save_room_snapshot(room)

    await query.edit_message_text(
        f"🚀 **게임 생성!**\n"
        f"방 코드: `{code}`\n"
        f"플레이어: {len(room.players)}명\n"
        f"마피아: {mafia_count}명",
        parse_mode="Markdown"
    )

    for uid, player in room.players.items():

        if not player["is_bot"]:

            await send_safe(
                context,
                uid,
                f"🎭 **직업 배정 완료**\n\n"
                f"당신의 직업: **[{player['role']}]**",
            )

    buttons = [[
        InlineKeyboardButton(
            "⏭ 광고 설정 건너뛰기",
            callback_data=f"skipad_{code}"
        )
    ]]

    await send_safe(
        context,
        room.host_id,
        "📢 **광고자 링크 설정**\n\n"
        "광고자가 사용할 링크/문구를 보내주세요.",
        InlineKeyboardMarkup(buttons)
    )


# ============================================================
# 12. 마피아 수 설정
# ============================================================

async def set_mafia_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if len(context.args) < 2:
        return

    code = context.args[0].upper()

    try:
        count = int(context.args[1])
    except ValueError:
        return

    room = rooms.get(code)

    if not room:
        return

    if room.host_id != update.effective_user.id:
        await update.message.reply_text("❌ 방장만 마피아 수를 설정할 수 있습니다.")
        return

    if room.state != "WAITING":
        await update.message.reply_text("❌ 대기 로비에서만 설정할 수 있습니다.")
        return

    room.target_mafia_count = max(
        1,
        min(4, count)
    )
    store.save_room_snapshot(room)

    await update.message.reply_text(
        f"😈 마피아 수: **{room.target_mafia_count}명**",
        parse_mode="Markdown"
    )


# ============================================================
# 13. 게임 시작
# ============================================================

async def start_game(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not context.args:
        return

    code = context.args[0].upper()

    room = rooms.get(code)

    if not room:
        return

    if len(room.players) < 3:

        await update.message.reply_text(
            "❌ 최소 3명 필요."
        )

        return

    if room.state != "WAITING":
        await update.message.reply_text("❌ 이 방은 시작할 수 없는 상태입니다.")
        return

    if room.host_id != update.effective_user.id:
        await update.message.reply_text("❌ 방장만 게임을 시작할 수 있습니다.")
        return

    room.assign_roles()
    room.state = "SETTING_AD"
    store.save_room_snapshot(room)

    for uid, player in room.players.items():

        if not player["is_bot"]:

            await send_safe(
                context,
                uid,
                f"🎭 **직업 배정!**\n\n"
                f"당신의 직업: **[{player['role']}]**"
            )

    buttons = [[
        InlineKeyboardButton(
            "⏭ 건너뛰기",
            callback_data=f"skipad_{code}"
        )
    ]]

    await send_safe(
        context,
        room.host_id,
        "📢 **광고 설정**\n"
        "광고자가 사용할 링크/문구를 입력하세요.",
        InlineKeyboardMarkup(buttons)
    )


# ============================================================
# 14. 광고 설정
# ============================================================

async def skip_ad_cb(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    code = query.data.split("_")[1]

    room = rooms.get(code)

    if not room:
        return

    if query.from_user.id != room.host_id:
        await query.answer("방장만 시작 설정을 완료할 수 있습니다.", show_alert=True)
        return

    room.ad_link = (
        "유튜브 [구독과 좋아요] 부탁드립니다!!"
    )

    room.state = "WAITING_NIGHT"
    store.save_room_snapshot(room)

    await query.edit_message_text(
        "⏭ 광고 설정 완료!"
    )

    await broadcast_all(
        context,
        room,
        "🚀 **게임 시작!**\n\n"
        "🌙 밤이 찾아옵니다..."
    )

    await asyncio.sleep(1)

    await start_night(
        context,
        room
    )


# ============================================================
# 15. 랜덤 사건
# ============================================================

def generate_random_event(room):

    events = [
        "정전",
        "수상한_발자국",
        "익명의_제보",
        "마을_축제",
        "CCTV_복구",
        "검은_고양이",
        "아무일도_없음",
    ]

    event = random.choice(events)

    room.current_event = event

    room.event_suspicion.clear()

    alive = list(
        room.get_alive_players().keys()
    )

    if event == "정전":

        return (
            "⚡ **[랜덤 사건] 정전 발생!**\n"
            "밤새 마을 전체가 깜깜했습니다."
        )

    if event == "수상한_발자국":

        if alive:

            target = random.choice(alive)

            room.event_suspicion[target] = 18

            name = room.players[target]["name"]

            return (
                "👣 **[랜덤 사건] 수상한 발자국!**\n"
                f"누군가 [{name}] 근처에서 "
                "수상한 발자국을 발견했습니다."
            )

    if event == "익명의_제보":

        mafia_exists = bool(
            room.alive_mafias()
        )

        if mafia_exists:

            return (
                "📨 **[랜덤 사건] 익명의 제보!**\n"
                "마을 어딘가에 마피아가 아직 숨어 있다는 "
                "익명 제보가 도착했습니다."
            )

        return (
            "📨 **[랜덤 사건] 익명의 제보!**\n"
            "하지만 내용이 의미가 없습니다."
        )

    if event == "마을_축제":

        return (
            "🎪 **[랜덤 사건] 마을 축제!**\n"
            "오늘만큼은 모두가 평소보다 말이 많아집니다."
        )

    if event == "CCTV_복구":

        candidates = [
            uid
            for uid in alive
            if uid not in room.public_known_roles
        ]

        if candidates:

            target = random.choice(candidates)

            name = room.players[target]["name"]

            return (
                "📹 **[랜덤 사건] CCTV 복구!**\n"
                f"CCTV에 [{name}]의 모습이 찍혔습니다."
            )

    if event == "검은_고양이":

        return (
            "🐈 **[랜덤 사건] 검은 고양이 출현!**\n"
            "고양이가 누군가의 발밑을 지나갔습니다..."
        )

    return (
        "🌤️ **[랜덤 사건] 아무 일도 없었습니다.**\n"
        "이게 오히려 제일 수상한데?"
    )


# ============================================================
# 16. 밤 시작
# ============================================================

async def start_night(
    context,
    room
):

    if room.state == "GAME_OVER":
        return

    room.state = "NIGHT"

    room.day += 1

    room.reset_night()
    store.save_room_snapshot(room)

    alive = room.get_alive_players()

    await broadcast_all(
        context,
        room,
        f"\n🌙 **[{room.day}번째 밤]**\n"
        "각자의 능력을 사용하세요."
    )

    # --------------------------------------------------------
    # 영매
    # --------------------------------------------------------

    for uid, player in alive.items():

        if player["role"] != "영매":
            continue

        if room.last_dead:

            room.medium_memory = (
                room.last_dead.copy()
            )

            if not player["is_bot"]:

                dead = room.last_dead

                await send_safe(
                    context,
                    uid,
                    f"👻 **[영매 결과]**\n\n"
                    f"최근 사망자: [{dead['name']}]\n"
                    f"정체: **[{dead['role']}]**"
                )

    # --------------------------------------------------------
    # 필요한 행동 목록
    # --------------------------------------------------------

    for uid, player in alive.items():

        role = player["role"]

        needs_action = role in [
            "마피아",
            "의사",
            "경찰",
            "기자",
            "건달",
        ]

        if needs_action:
            room.night_pending.add(uid)

    # --------------------------------------------------------
    # 봇 자동 행동
    # --------------------------------------------------------

    for uid, player in alive.items():

        if not player["is_bot"]:
            continue

        role = player["role"]

        others = [
            x
            for x in alive
            if x != uid
        ]

        if role == "마피아":

            targets = [
                x
                for x in others
                if alive[x]["role"] != "마피아"
            ]

            if targets:

                target = MafiaAI.choose_target(
                    uid,
                    role,
                    room,
                    targets
                )

                room.mafia_choices[uid] = target

                room.night_pending.discard(uid)

        elif role == "의사":

            target = MafiaAI.choose_target(
                uid,
                role,
                room,
                list(alive.keys())
            )

            room.doctor_target = target

            room.night_pending.discard(uid)

        elif role == "경찰":

            if others:

                target = MafiaAI.choose_target(
                    uid,
                    role,
                    room,
                    others
                )

                room.police_target = target

                room.night_pending.discard(uid)

        elif role == "기자":

            if others:

                target = MafiaAI.choose_target(
                    uid,
                    role,
                    room,
                    others
                )

                room.reporter_target = target

                room.night_pending.discard(uid)

        elif role == "건달":

            if others:

                target = MafiaAI.choose_target(
                    uid,
                    role,
                    room,
                    others
                )

                room.gangster_target = target

                room.night_pending.discard(uid)

    # --------------------------------------------------------
    # 인간 플레이어 버튼
    # --------------------------------------------------------

    for uid, player in alive.items():

        if player["is_bot"]:
            continue

        role = player["role"]

        others = [
            x
            for x in alive
            if x != uid
        ]

        buttons = []

        if role == "마피아":

            buttons = [
                [
                    InlineKeyboardButton(
                        alive[x]["name"],
                        callback_data=f"kill_{x}_{room.room_code}"
                    )
                ]
                for x in others
                if alive[x]["role"] != "마피아"
            ]

            await send_safe(
                context,
                uid,
                "🩸 **처단할 대상을 선택하세요.**",
                InlineKeyboardMarkup(buttons)
            )

        elif role == "의사":

            buttons = [
                [
                    InlineKeyboardButton(
                        alive[x]["name"],
                        callback_data=f"heal_{x}_{room.room_code}"
                    )
                ]
                for x in alive
            ]

            await send_safe(
                context,
                uid,
                "🚑 **치료할 대상을 선택하세요.**",
                InlineKeyboardMarkup(buttons)
            )

        elif role == "경찰":

            buttons = [
                [
                    InlineKeyboardButton(
                        alive[x]["name"],
                        callback_data=f"police_{x}_{room.room_code}"
                    )
                ]
                for x in others
            ]

            await send_safe(
                context,
                uid,
                "🔍 **조사할 대상을 선택하세요.**",
                InlineKeyboardMarkup(buttons)
            )

        elif role == "기자":

            buttons = [
                [
                    InlineKeyboardButton(
                        alive[x]["name"],
                        callback_data=f"reporter_{x}_{room.room_code}"
                    )
                ]
                for x in others
            ]

            await send_safe(
                context,
                uid,
                "📰 **취재할 대상을 선택하세요.**",
                InlineKeyboardMarkup(buttons)
            )

        elif role == "건달":

            buttons = [
                [
                    InlineKeyboardButton(
                        alive[x]["name"],
                        callback_data=f"gangster_{x}_{room.room_code}"
                    )
                ]
                for x in others
            ]

            await send_safe(
                context,
                uid,
                "👊 **내일 투표를 막을 대상을 선택하세요.**",
                InlineKeyboardMarkup(buttons)
            )

    # 인간 행동이 필요 없으면 즉시 판정
    await try_resolve_night(
        context,
        room
    )


# ============================================================
# 17. 밤 행동 콜백
# ============================================================

async def night_action_cb(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    parts = query.data.split("_")

    action = parts[0]

    target_id = int(parts[1])

    code = parts[2]

    room = rooms.get(code)

    if not room:
        return

    if room.state != "NIGHT":
        return

    voter_id = query.from_user.id

    if voter_id not in room.players:
        return

    player = room.players[voter_id]

    if not player["alive"]:
        return

    target = room.players.get(target_id)

    if not target or not target["alive"]:
        await query.answer(
            "이미 죽었거나 없는 플레이어입니다.",
            show_alert=True
        )
        return

    # --------------------------------------------------------
    # 행동 처리
    # --------------------------------------------------------

    if action == "kill":

        if player["role"] != "마피아":
            return

        room.mafia_choices[voter_id] = target_id

    elif action == "heal":

        if player["role"] != "의사":
            return

        room.doctor_target = target_id

    elif action == "police":

        if player["role"] != "경찰":
            return

        room.police_target = target_id

        role = target["role"]

        result = (
            "😈 마피아"
            if role == "마피아"
            else "🙂 시민/특수직"
        )

        await query.edit_message_text(
            f"🔍 **조사 완료**\n\n"
            f"[{target['name']}] → {result}"
        )

    elif action == "reporter":

        if player["role"] != "기자":
            return

        room.reporter_target = target_id

    elif action == "gangster":

        if player["role"] != "건달":
            return

        room.gangster_target = target_id

    else:
        return

    room.night_pending.discard(
        voter_id
    )

    if action != "police":

        try:
            await query.edit_message_text(
                "✅ 선택 완료!"
            )
        except Exception:
            pass

    await try_resolve_night(
        context,
        room
    )


# ============================================================
# 18. 밤 판정
# ============================================================

async def try_resolve_night(
    context,
    room
):

    if room.state != "NIGHT":
        return

    if room.night_pending:
        return

    await resolve_night(
        context,
        room
    )


async def resolve_night(
    context,
    room
):

    if room.state != "NIGHT":
        return

    room.state = "RESOLVING_NIGHT"

    alive_before = room.get_alive_players()

    mafia_target = room.choose_mafia_target()

    room.silenced_target = (
        room.gangster_target
    )

    message_parts = []

    # --------------------------------------------------------
    # 기자
    # --------------------------------------------------------

    if room.reporter_target is not None:

        target = room.reporter_target

        if target in room.players:

            role = room.players[target]["role"]

            room.public_known_roles[target] = role

            name = room.players[target]["name"]

            message_parts.append(
                f"📰 **[기자 속보]**\n"
                f"[{name}]의 정체는 "
                f"**[{role}]** 입니다!"
            )

    # --------------------------------------------------------
    # 건달
    # --------------------------------------------------------

    if room.silenced_target is not None:

        if room.silenced_target in room.players:

            name = room.players[
                room.silenced_target
            ]["name"]

            message_parts.append(
                f"👊 **[건달]**\n"
                f"[{name}]은 오늘 투표할 수 없습니다."
            )

    # --------------------------------------------------------
    # 마피아 공격
    # --------------------------------------------------------

    if mafia_target is not None:

        if mafia_target not in room.players:
            mafia_target = None

    if mafia_target is not None:

        target = room.players[
            mafia_target
        ]

        name = target["name"]

        role = target["role"]

        # 광고자 보호
        if (
            role == "광고자"
            and room.spammer_protected
            and not room.spammer_gave_up
        ):

            room.spammer_gave_up = True

            message_parts.append(
                f"🛡️ **[개발자의 가호]**\n"
                f"광고자 [{name}]가 살아남았습니다!\n"
                f"🤖 AI: 핵 쓰네 ㅡㅡ"
            )

        # 의사
        elif room.doctor_target == mafia_target:

            message_parts.append(
                f"🚑 **[의사의 치료]**\n"
                f"[{name}]가 치료받아 살아남았습니다!"
            )

        # 유령 예측
        elif mafia_target in room.ghost_predictions.values():

            message_parts.append(
                f"👻 **[유령의 예측]**\n"
                f"[{name}]에게 죽음의 예측이 적중했습니다."
            )

        else:

            target["alive"] = False

            room.last_dead = {
                "uid": mafia_target,
                "name": name,
                "role": role,
            }

            message_parts.append(
                f"🩸 **[밤의 희생자]**\n"
                f"[{name}] 사망...\n"
                f"정체: **[{role}]**"
            )

            room.add_highlight(
                f"{name}이(가) 밤에 사망"
            )

            # 테러리스트
            if role == "테러리스트":

                alive_mafias = room.alive_mafias()

                if alive_mafias:

                    bombed = random.choice(
                        alive_mafias
                    )

                    bombed_player = room.players[
                        bombed
                    ]

                    bombed_player["alive"] = False

                    room.add_highlight(
                        f"테러리스트 {name}의 자폭"
                    )

                    message_parts.append(
                        f"💣 **[테러리스트 자폭!]**\n"
                        f"[{name}]가 죽으면서 "
                        f"마피아 [{bombed_player['name']}]까지 "
                        f"같이 데려갔습니다!"
                    )

    else:

        message_parts.append(
            "🌅 **밤에 아무도 죽지 않았습니다.**"
        )

    # --------------------------------------------------------
    # 랜덤 사건
    # --------------------------------------------------------

    if room.settings.get("random_events", True):
        event_text = generate_random_event(room)
        message_parts.append(event_text)
    else:
        room.current_event = None
        room.event_suspicion.clear()

    await broadcast_all(
        context,
        room,
        "☀️ **아침이 밝았습니다!**\n\n"
        + "\n\n".join(message_parts)
    )

    # --------------------------------------------------------
    # 죽은 봇 유언
    # --------------------------------------------------------

    ghosts = room.get_ghost_players()

    for uid, player in ghosts.items():

        if player["is_bot"]:

            if random.random() < 0.75:

                await broadcast_ghosts(
                    context,
                    room,
                    DynamicChatEngine.ghost(
                        player["name"]
                    )
                )

    await asyncio.sleep(2)

    await check_game_end(
        context,
        room
    )

    if room.state == "RESOLVING_NIGHT":

        await start_day_vote(
            context,
            room
        )


# ============================================================
# 19. 게임 종료 판정
# ============================================================

async def check_game_end(
    context,
    room
):

    alive = room.get_alive_players()

    mafia = sum(
        1
        for player in alive.values()
        if player["role"] == "마피아"
    )

    citizens = len(alive) - mafia

    if mafia == 0:

        await finish_game(
            context,
            room,
            "시민"
        )

        return

    if mafia >= citizens:

        await finish_game(
            context,
            room,
            "마피아"
        )

        return


async def finish_game(
    context,
    room,
    winner
):

    if room.state == "GAME_OVER":
        return

    room.state = "GAME_OVER"

    # 통계
    for player in room.players.values():

        role = player["role"]

        if role == "마피아":
            team = "마피아"
        else:
            team = "시민"

        if team == winner:
            room.stats[
                f"{player['name']}_승리"
            ] += 1
        else:
            room.stats[
                f"{player['name']}_패배"
            ] += 1

    game_id = store.record_game(room, winner)

    if winner == "시민":

        result = (
            "🎉 **시민 진영 승리!** 🏆\n"
            "마을에 평화가 돌아왔습니다."
        )

    else:

        result = (
            "🩸 **마피아 진영 승리!** 😈\n"
            "마을은 마피아의 손에 넘어갔습니다."
        )

    roles = []

    for player in room.players.values():

        roles.append(
            f"• {player['name']} → "
            f"**{player['role']}**"
        )

    highlight_text = ""

    if room.highlights:

        highlight_text = (
            "\n\n🔥 **게임 하이라이트**\n"
            + "\n".join(
                f"• {x}"
                for x in room.highlights[-8:]
            )
        )

    await broadcast_all(
        context,
        room,
        f"{result}\n\n"
        f"🎭 **최종 직업 공개**\n"
        + "\n".join(roles)
        + highlight_text
        + f"\n\n📊 게임 기록 번호: **#{game_id}** · `/profile`, `/rank`에서 전적을 확인하세요."
    )


# ============================================================
# 20. 낮 토론
# ============================================================

async def start_day_vote(
    context,
    room
):

    room.state = "DAY_VOTE"

    room.votes = {}

    alive = room.get_alive_players()

    await broadcast_all(
        context,
        room,
        f"🗣 **[{room.day}일차 낮 토론 시작!]**\n\n"
        "봇들이 토론을 시작합니다..."
    )

    await asyncio.sleep(1)

    # --------------------------------------------------------
    # 영매 발표
    # --------------------------------------------------------

    if room.medium_memory:

        dead = room.medium_memory

        medium_bots = [
            player
            for player in alive.values()
            if player["is_bot"]
            and player["role"] == "영매"
        ]

        for medium in medium_bots:

            lines = [
                f"👻 저 영매입니다. 어제 죽은 "
                f"[{dead['name']}] 직업은 [{dead['role']}]임.",

                f"📢 영매 결과 공개합니다. "
                f"[{dead['name']}] = [{dead['role']}].",

                f"방금 영매 능력 썼는데 "
                f"[{dead['name']}] 정체 [{dead['role']}] 나왔습니다.",
            ]

            await broadcast_all(
                context,
                room,
                f"🤖 **[{medium['name']}]**\n"
                + random.choice(lines)
            )

        room.medium_memory = None

    # --------------------------------------------------------
    # 광고자
    # --------------------------------------------------------

    for uid, player in alive.items():

        if player["role"] == "광고자":

            await broadcast_all(
                context,
                room,
                f"📢 **[{player['name']}] 광고자**\n"
                f"다들 잠깐만요!!\n"
                f"👉 {room.ad_link}"
            )

    # --------------------------------------------------------
    # 봇 토론
    # --------------------------------------------------------

    bot_players = [
        (uid, player)
        for uid, player in alive.items()
        if player["is_bot"]
    ]

    random.shuffle(bot_players)

    for uid, player in bot_players:

        speech = MafiaAI.speech(
            uid,
            room
        )

        await broadcast_all(
            context,
            room,
            f"🤖 **[{player['name']}]**\n"
            f"{speech}"
        )

        await asyncio.sleep(
            random.uniform(
                0.3,
                0.9
            )
        )

    # --------------------------------------------------------
    # 인간 투표
    # --------------------------------------------------------

    for uid, player in alive.items():

        if player["is_bot"]:
            continue

        buttons = [
            [
                InlineKeyboardButton(
                    p["name"],
                    callback_data=f"vote_{target}_{room.room_code}"
                )
            ]
            for target, p in alive.items()
            if target != uid
        ]

        await send_safe(
            context,
            uid,
            "🗳 **처형할 사람을 선택하세요.**",
            InlineKeyboardMarkup(buttons)
        )

    # --------------------------------------------------------
    # 봇 투표
    # --------------------------------------------------------

    for uid, player in alive.items():

        if not player["is_bot"]:
            continue

        if uid == room.silenced_target:
            continue

        candidates = [
            x
            for x in alive
            if x != uid
        ]

        if not candidates:
            continue

        target = MafiaAI.choose_target(
            uid,
            player["role"],
            room,
            candidates
        )

        room.votes[uid] = target

        # AI 기억
        player["personality"]["anger"].setdefault(
            target,
            0
        )

    eligible = sum(
        1
        for uid in alive
        if uid != room.silenced_target
    )

    if len(room.votes) >= eligible:

        await process_day_vote_result(
            context,
            room
        )


# ============================================================
# 21. 낮 투표 콜백
# ============================================================

async def day_vote_cb(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    parts = query.data.split("_")

    target_id = int(parts[1])

    code = parts[2]

    room = rooms.get(code)

    if not room:
        return

    voter = query.from_user.id

    if room.state != "DAY_VOTE":
        return

    if voter == room.silenced_target:

        await query.answer(
            "👊 건달에게 맞아서 투표할 수 없습니다!",
            show_alert=True
        )

        return

    if voter not in room.players:
        return

    if not room.players[voter]["alive"]:
        return

    room.votes[voter] = target_id

    room.previous_votes[voter] = target_id

    try:

        await query.edit_message_text(
            "🗳 투표 완료!"
        )

    except Exception:
        pass

    eligible = sum(
        1
        for uid in room.get_alive_players()
        if uid != room.silenced_target
    )

    if len(room.votes) >= eligible:

        await process_day_vote_result(
            context,
            room
        )


# ============================================================
# 22. 낮 투표 결과
# ============================================================

async def process_day_vote_result(
    context,
    room
):

    if room.state != "DAY_VOTE":
        return

    room.state = "COUNTING"

    vote_counts = Counter()

    for voter, target in room.votes.items():

        if voter not in room.players:
            continue

        if not room.players[voter]["alive"]:
            continue

        weight = (
            2
            if room.players[voter]["role"] == "정치인"
            else 1
        )

        vote_counts[target] += weight

    if not vote_counts:

        await start_night(
            context,
            room
        )

        return

    top_value = max(
        vote_counts.values()
    )

    targets = [
        uid
        for uid, count in vote_counts.items()
        if count == top_value
    ]

    if len(targets) > 1:

        names = [
            room.players[x]["name"]
            for x in targets
        ]

        await broadcast_all(
            context,
            room,
            f"⚖️ **동표 발생!**\n"
            f"대상: {', '.join(names)}\n\n"
            f"🔥 재투표합니다!"
        )

        await asyncio.sleep(2)

        await start_revote(
            context,
            room,
            targets
        )

        return

    target_id = targets[0]

    room.defense_target = target_id

    target = room.players[target_id]

    # 정치인
    if target["role"] == "정치인":

        await broadcast_all(
            context,
            room,
            f"👑 **[정치인 패시브]**\n"
            f"[{target['name']}]는 정치인이라 "
            f"처형되지 않습니다!"
        )

        await asyncio.sleep(2)

        await start_night(
            context,
            room
        )

        return

    # 변론
    room.state = "DEFENSE"

    if target["is_bot"]:

        speech = DynamicChatEngine.defense(
            target["name"],
            target.get("personality")
        )

        await broadcast_all(
            context,
            room,
            f"🚨 **최후의 변론**\n\n"
            f"🤖 [{target['name']}]\n"
            f"{speech}"
        )

    else:

        await broadcast_all(
            context,
            room,
            f"🚨 **[{target['name']}] 최후의 변론!**\n"
            f"8초 동안 해명하세요."
        )

        await asyncio.sleep(8)

    await start_final_vote(
        context,
        room
    )


# ============================================================
# 23. 동률 재투표
# ============================================================

async def start_revote(
    context,
    room,
    candidates
):

    room.state = "REVOTE"

    room.votes = {}

    alive = room.get_alive_players()

    # 봇
    for uid, player in alive.items():

        if not player["is_bot"]:
            continue

        if uid == room.silenced_target:
            continue

        target = random.choice(candidates)

        room.votes[uid] = target

    # 인간
    for uid, player in alive.items():

        if player["is_bot"]:
            continue

        if uid == room.silenced_target:
            continue

        buttons = [
            [
                InlineKeyboardButton(
                    room.players[target]["name"],
                    callback_data=f"revote_{target}_{room.room_code}"
                )
            ]
            for target in candidates
        ]

        await send_safe(
            context,
            uid,
            "🔥 **동률 재투표!**\n"
            "이번에는 아래 후보 중 선택하세요.",
            InlineKeyboardMarkup(buttons)
        )

    eligible = sum(
        1
        for uid in alive
        if uid != room.silenced_target
    )

    if len(room.votes) >= eligible:

        await finish_revote(
            context,
            room,
            candidates
        )


async def revote_cb(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    parts = query.data.split("_")

    target = int(parts[1])

    code = parts[2]

    room = rooms.get(code)

    if not room:
        return

    voter = query.from_user.id

    if room.state != "REVOTE":
        return

    if voter == room.silenced_target:

        await query.answer(
            "👊 투표 불가!",
            show_alert=True
        )

        return

    room.votes[voter] = target

    try:
        await query.edit_message_text(
            "🔥 재투표 완료!"
        )
    except Exception:
        pass

    eligible = sum(
        1
        for uid in room.get_alive_players()
        if uid != room.silenced_target
    )

    candidates = list(
        {
            target
            for target in room.votes.values()
        }
    )

    if len(room.votes) >= eligible:

        await finish_revote(
            context,
            room,
            candidates
        )


async def finish_revote(
    context,
    room,
    candidates
):

    counts = Counter(
        room.votes.values()
    )

    if not counts:
        await start_night(context, room)
        return

    maximum = max(
        counts.values()
    )

    winners = [
        uid
        for uid, count in counts.items()
        if count == maximum
    ]

    if len(winners) != 1:

        await broadcast_all(
            context,
            room,
            "⚖️ 재투표도 동률!\n"
            "오늘은 아무도 처형되지 않습니다."
        )

        await asyncio.sleep(2)

        await start_night(
            context,
            room
        )

        return

    room.defense_target = winners[0]

    await broadcast_all(
        context,
        room,
        f"🎯 재투표 결과 "
        f"[{room.players[winners[0]]['name']}] 결정!"
    )

    await asyncio.sleep(1)

    await start_final_vote(
        context,
        room
    )


# ============================================================
# 24. 최종 찬반 투표
# ============================================================

async def start_final_vote(
    context,
    room
):

    room.state = "FINAL_VOTE"

    room.final_votes = {}

    target_id = room.defense_target

    target_name = room.players[
        target_id
    ]["name"]

    await broadcast_all(
        context,
        room,
        f"⚖️ **[{target_name}] 최종 처형 투표!**\n\n"
        f"💀 처형 vs 🕊 살려줌"
    )

    alive = room.get_alive_players()

    for uid, player in alive.items():

        if uid in [
            target_id,
            room.silenced_target
        ]:
            continue

        if player["is_bot"]:

            # 봇은 조금 더 논리적으로 판단
            role = room.players[target_id]["role"]

            suspicion = MafiaAI.calculate_suspicion(
                uid,
                room
            ).get(
                target_id,
                30
            )

            if role == "마피아":
                decision = "KILL"
            elif suspicion >= 65:
                decision = "KILL"
            else:
                decision = random.choice([
                    "KILL",
                    "SAVE",
                    "SAVE"
                ])

            room.final_votes[uid] = decision

        else:

            buttons = [[
                InlineKeyboardButton(
                    "💀 처형",
                    callback_data=f"final_KILL_{room.room_code}"
                ),
                InlineKeyboardButton(
                    "🕊 살려줌",
                    callback_data=f"final_SAVE_{room.room_code}"
                )
            ]]

            await send_safe(
                context,
                uid,
                f"[{target_name}] 처형?",
                InlineKeyboardMarkup(buttons)
            )

    eligible = sum(
        1
        for uid in alive
        if uid != target_id
        and uid != room.silenced_target
    )

    if len(room.final_votes) >= eligible:

        await process_final_vote_result(
            context,
            room
        )


async def final_vote_cb(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    parts = query.data.split("_")

    decision = parts[1]

    code = parts[2]

    room = rooms.get(code)

    if not room:
        return

    voter = query.from_user.id

    if room.state != "FINAL_VOTE":
        return

    if voter == room.silenced_target:

        await query.answer(
            "👊 투표 불가!",
            show_alert=True
        )

        return

    room.final_votes[voter] = decision

    try:
        await query.edit_message_text(
            "⚖️ 최종 투표 완료!"
        )
    except Exception:
        pass

    eligible = sum(
        1
        for uid in room.get_alive_players()
        if uid != room.defense_target
        and uid != room.silenced_target
    )

    if len(room.final_votes) >= eligible:

        await process_final_vote_result(
            context,
            room
        )


# ============================================================
# 25. 최종 투표 결과
# ============================================================

async def process_final_vote_result(
    context,
    room
):

    if room.state != "FINAL_VOTE":
        return

    room.state = "EXECUTING"

    kill = sum(
        1
        for value in room.final_votes.values()
        if value == "KILL"
    )

    save = sum(
        1
        for value in room.final_votes.values()
        if value == "SAVE"
    )

    target_id = room.defense_target

    target = room.players[
        target_id
    ]

    name = target["name"]

    role = target["role"]

    if kill > save:

        # 광고자 무적
        if (
            role == "광고자"
            and room.spammer_protected
            and not room.spammer_gave_up
        ):

            room.spammer_gave_up = True

            await broadcast_all(
                context,
                room,
                f"🛡️ **개발자의 가호 발동!**\n"
                f"[{name}] 처형 무효!\n"
                f"🤖 AI: 아 핵 쓰네 ㅡㅡ"
            )

        else:

            target["alive"] = False

            room.last_dead = {
                "uid": target_id,
                "name": name,
                "role": role,
            }

            room.add_highlight(
                f"{name} 처형됨"
            )

            await broadcast_all(
                context,
                room,
                f"💀 **[처형 완료]**\n\n"
                f"찬성 {kill} : 반대 {save}\n\n"
                f"[{name}] 처형!\n"
                f"정체: **[{role}]**"
            )

            # 테러리스트
            if role == "테러리스트":

                kill_voters = [
                    uid
                    for uid, vote in room.final_votes.items()
                    if vote == "KILL"
                    and room.players[uid]["alive"]
                ]

                if kill_voters:

                    victim = random.choice(
                        kill_voters
                    )

                    room.players[
                        victim
                    ]["alive"] = False

                    victim_name = room.players[
                        victim
                    ]["name"]

                    victim_role = room.players[
                        victim
                    ]["role"]

                    room.last_dead = {
                        "uid": victim,
                        "name": victim_name,
                        "role": victim_role,
                    }

                    room.add_highlight(
                        f"테러리스트가 {victim_name}을 데려감"
                    )

                    await broadcast_all(
                        context,
                        room,
                        f"💣 **[테러리스트 자폭!]**\n"
                        f"[{name}]가 죽으면서 "
                        f"[{victim_name}]까지 폭사시켰습니다!\n"
                        f"정체: [{victim_role}]"
                    )

    else:

        await broadcast_all(
            context,
            room,
            f"🕊 **[처형 부결]**\n"
            f"[{name}] 생존!\n"
            f"찬성 {kill} : 반대 {save}"
        )

    await asyncio.sleep(2)

    await check_game_end(
        context,
        room
    )

    if room.state == "EXECUTING":

        await start_night(
            context,
            room
        )


# ============================================================
# 26. 개발자 명령
# ============================================================

async def sudo_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if len(context.args) < 2:
        return

    password = context.args[0]

    action = context.args[1]

    if password != DEV_PASSWORD:
        return

    user_id = update.effective_user.id

    # 무적
    if action == "무적":

        if len(context.args) < 3:
            return

        code = context.args[2].upper()

        room = rooms.get(code)

        if not room:
            return

        room.spammer_protected = (
            not room.spammer_protected
        )

        status = (
            "활성화 🛡️"
            if room.spammer_protected
            else "비활성화 ❌"
        )

        await update.message.reply_text(
            f"💻 광고자 무적: **{status}**",
            parse_mode="Markdown"
        )

        return

    # 투시
    if action == "투시":

        if len(context.args) < 3:
            return

        code = context.args[2].upper()

        room = rooms.get(code)

        if not room:
            return

        text = "\n".join(
            f"• {p['name']}: **{p['role']}**"
            for p in room.players.values()
        )

        await update.message.reply_text(
            "👁️ **전체 직업 공개**\n\n"
            + text,
            parse_mode="Markdown"
        )

        return

    # 강제 직업
    valid_roles = [
        "마피아",
        "경찰",
        "의사",
        "기자",
        "정치인",
        "건달",
        "영매",
        "테러리스트",
        "광고자",
        "시민",
    ]

    if action in valid_roles:

        dev_forced_roles[user_id] = action

        await update.message.reply_text(
            f"💻 다음 게임 직업: **[{action}]**",
            parse_mode="Markdown"
        )


# ============================================================
# 27. 일반 채팅
# ============================================================

async def handle_user_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    user_id = update.effective_user.id

    text = update.message.text

    for room in rooms.values():

        if user_id not in room.players:
            continue

        player = room.players[
            user_id
        ]

        # 광고 설정
        if (
            room.state == "SETTING_AD"
            and user_id == room.host_id
        ):

            room.ad_link = text[:500]

            room.state = "WAITING_NIGHT"
            store.save_room_snapshot(room)

            await send_safe(
                context,
                user_id,
                f"✅ 광고 적용 완료!\n"
                f"👉 {text}"
            )

            await broadcast_all(
                context,
                room,
                "🚀 **설정 완료!**\n"
                "🌙 밤이 찾아옵니다..."
            )

            await asyncio.sleep(1)

            await start_night(
                context,
                room
            )

            return

        # 마피아 자백 감지
        confessions = [
            "나 마피아",
            "내가 마피아",
            "저 마피아",
            "제가 마피아",
            "난 마피아",
            "나는 마피아",
        ]

        if (
            any(
                x in text
                for x in confessions
            )
            and "아님" not in text
            and "아니" not in text
        ):

            room.self_proclaimed_mafias.add(
                user_id
            )

            room.add_highlight(
                f"{player['name']} 마피아 자백"
            )

        # 채팅
        if player["alive"]:

            await broadcast_alive(
                context,
                room,
                f"💬 **[{player['name']}]**: {text}",
                sender_id=user_id
            )

        else:

            await broadcast_ghosts(
                context,
                room,
                f"👻 **[저승 - {player['name']}]**: {text}",
                sender_id=user_id
            )

        return


# ============================================================
# 28. 메인
# ============================================================


async def error_handler(update, context):
    """처리되지 않은 예외를 로그에 남기되, 봇 프로세스는 계속 실행한다."""
    logging.getLogger(__name__).exception(
        "처리되지 않은 업데이트 예외: %s", context.error
    )


def main():

    if (
        not TELEGRAM_TOKEN
        or TELEGRAM_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN"
    ):

        print(
            "❌ TELEGRAM_TOKEN을 설정하세요."
        )

        return

    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )

    app = (
        Application
        .builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    # 연동
    app.add_handler(
        MessageHandler(
            filters.Regex(r"^/연동"),
            link_account_cmd
        )
    )

    app.add_handler(
        CommandHandler(
            "link",
            link_account_cmd
        )
    )

    # 기본
    app.add_handler(
        CommandHandler(
            ["start", "help"],
            start_cmd
        )
    )

    app.add_handler(
        CommandHandler(
            ["fast", "fast_start"],
            fast_start_cmd
        )
    )

    app.add_handler(
        CommandHandler(
            "create",
            create_room
        )
    )

    app.add_handler(
        CommandHandler(
            "join",
            join_room
        )
    )

    app.add_handler(
        CommandHandler(
            "add_bot",
            add_bot
        )
    )

    app.add_handler(
        CommandHandler(
            "start_game",
            start_game
        )
    )

    app.add_handler(
        CommandHandler(
            "set_mafia",
            set_mafia_cmd
        )
    )

    app.add_handler(
        CommandHandler(
            "sudo",
            sudo_cmd
        )
    )

    # 빠른 시작
    app.add_handler(
        CallbackQueryHandler(
            fast_bot_cb,
            pattern=r"^fastbot_"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            fast_mafia_cb,
            pattern=r"^fastmaf_"
        )
    )

    # 광고
    app.add_handler(
        CallbackQueryHandler(
            skip_ad_cb,
            pattern=r"^skipad_"
        )
    )

    # 밤
    app.add_handler(
        CallbackQueryHandler(
            night_action_cb,
            pattern=r"^(kill|heal|police|reporter|gangster)_"
        )
    )

    # 낮
    app.add_handler(
        CallbackQueryHandler(
            day_vote_cb,
            pattern=r"^vote_"
        )
    )

    # 재투표
    app.add_handler(
        CallbackQueryHandler(
            revote_cb,
            pattern=r"^revote_"
        )
    )

    # 최종 투표
    app.add_handler(
        CallbackQueryHandler(
            final_vote_cb,
            pattern=r"^final_"
        )
    )

    # v3 운영·전적 명령어
    register_v3_handlers(
        app,
        rooms,
        store,
        add_ai_bot_to_room,
    )

    # 일반 채팅
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_user_chat
        )
    )

    app.add_error_handler(error_handler)

    print(
        "🔥 Mafia Bot v3 가동!"
    )

    print(
        "🤖 AI 성격 시스템"
    )

    print(
        "🎲 랜덤 사건 시스템"
    )

    print(
        "👻 유령 시스템"
    )

    print(
        "💣 테러리스트 시스템"
    )

    print(
        "🧠 개선된 밤 행동 동기화"
    )

    app.run_polling()


if __name__ == "__main__":
    main()