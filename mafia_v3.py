"""Mafia Bot v3의 영구 데이터와 운영 명령어 모듈.

이 모듈은 기존 게임 엔진을 유지하면서 SQLite 기반 전적, 랭킹, 로비 관리와
운영 편의 명령어를 추가한다. 모든 데이터베이스 연산은 표준 라이브러리만 사용한다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


ROLE_GUIDE: Dict[str, str] = {
    "마피아": "매일 밤 시민 진영 한 명을 처단합니다. 마피아끼리는 서로를 알 수 있습니다.",
    "시민": "특수 능력은 없지만 토론과 투표로 마피아를 찾아야 합니다.",
    "경찰": "매일 밤 한 명을 조사해 마피아인지 확인합니다.",
    "의사": "매일 밤 한 명을 치료합니다. 마피아의 공격 대상과 같으면 생존시킵니다.",
    "기자": "매일 밤 취재한 대상의 정보가 다음 날 공개될 수 있습니다.",
    "정치인": "처형 위기에서 특별한 영향력을 발휘하는 시민 진영 직업입니다.",
    "건달": "매일 밤 한 명을 지목해 다음 날 투표를 막습니다.",
    "영매": "최근 사망자의 직업을 확인합니다.",
    "테러리스트": "처형되면 자신과 함께할 대상을 선택할 수 있습니다.",
    "광고자": "낮 토론 중 방장이 설정한 문구를 알립니다.",
}


STATE_LABELS = {
    "WAITING": "대기 중",
    "SETTING_AD": "시작 설정 중",
    "WAITING_NIGHT": "첫날 밤 준비",
    "NIGHT": "밤 진행 중",
    "DAY_VOTE": "낮 투표 중",
    "COUNTING": "투표 집계 중",
    "REVOTE": "재투표 중",
    "FINAL_VOTE": "최종 투표 중",
    "EXECUTING": "처형 처리 중",
    "GAME_OVER": "게임 종료",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def display_name(name: str, fallback: str = "플레이어") -> str:
    """Telegram Markdown 문법을 깨뜨리지 않는 짧은 표시 이름을 만든다."""
    value = (name or fallback).replace("\n", " ").strip()
    for char in "`*_[]()":
        value = value.replace(char, "")
    return value[:24] or fallback


class MafiaStore:
    """SQLite로 사용자 전적과 완료 게임 기록을 관리한다."""

    def __init__(self, db_path: str | Path = "data/mafia_v3.sqlite3") -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    games INTEGER NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0,
                    losses INTEGER NOT NULL DEFAULT 0,
                    mafia_wins INTEGER NOT NULL DEFAULT 0,
                    citizen_wins INTEGER NOT NULL DEFAULT 0,
                    points INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS game_history (
                    game_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_code TEXT NOT NULL,
                    winner TEXT NOT NULL,
                    day_count INTEGER NOT NULL,
                    completed_at TEXT NOT NULL,
                    highlights TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS game_players (
                    game_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    team TEXT NOT NULL,
                    won INTEGER NOT NULL,
                    survived INTEGER NOT NULL,
                    PRIMARY KEY (game_id, user_id),
                    FOREIGN KEY (game_id) REFERENCES game_history(game_id)
                );

                CREATE TABLE IF NOT EXISTS room_snapshots (
                    room_code TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def touch_user(self, user_id: int, name: str) -> None:
        if user_id < 0:
            return
        cleaned = display_name(name)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, display_name, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    updated_at = excluded.updated_at
                """,
                (user_id, cleaned, utc_now()),
            )

    def save_room_snapshot(self, room: Any) -> None:
        """로비/진행 상황을 진단용 스냅샷으로 남긴다. 게임 복구 기능은 의도적으로 제공하지 않는다."""
        players = []
        for user_id, player in room.players.items():
            players.append(
                {
                    "id": user_id,
                    "name": display_name(player.get("name", "플레이어")),
                    "role": player.get("role"),
                    "alive": bool(player.get("alive", False)),
                    "bot": bool(player.get("is_bot", False)),
                }
            )
        payload = {
            "host_id": room.host_id,
            "day": room.day,
            "target_mafia_count": room.target_mafia_count,
            "settings": getattr(room, "settings", {}),
            "players": players,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO room_snapshots (room_code, state, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(room_code) DO UPDATE SET
                    state=excluded.state,
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (room.room_code, room.state, json.dumps(payload, ensure_ascii=False), utc_now()),
            )

    def remove_room_snapshot(self, room_code: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM room_snapshots WHERE room_code = ?", (room_code,))

    def record_game(self, room: Any, winner: str) -> int:
        """종료된 한 게임과 인간 플레이어의 전적을 원자적으로 기록한다."""
        highlights = json.dumps(list(room.highlights[-15:]), ensure_ascii=False)
        completed_at = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO game_history (room_code, winner, day_count, completed_at, highlights)
                VALUES (?, ?, ?, ?, ?)
                """,
                (room.room_code, winner, room.day, completed_at, highlights),
            )
            game_id = int(cursor.lastrowid)
            for user_id, player in room.players.items():
                if player.get("is_bot") or user_id < 0:
                    continue
                role = player.get("role") or "시민"
                team = "마피아" if role == "마피아" else "시민"
                won = int(team == winner)
                survived = int(bool(player.get("alive")))
                points = 15 if won else 4
                points += 2 if survived else 0
                conn.execute(
                    """
                    INSERT INTO users (user_id, display_name, games, wins, losses, mafia_wins,
                                       citizen_wins, points, updated_at)
                    VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        display_name=excluded.display_name,
                        games=users.games + 1,
                        wins=users.wins + excluded.wins,
                        losses=users.losses + excluded.losses,
                        mafia_wins=users.mafia_wins + excluded.mafia_wins,
                        citizen_wins=users.citizen_wins + excluded.citizen_wins,
                        points=users.points + excluded.points,
                        updated_at=excluded.updated_at
                    """,
                    (
                        user_id,
                        display_name(player.get("name", "플레이어")),
                        won,
                        1 - won,
                        int(won and team == "마피아"),
                        int(won and team == "시민"),
                        points,
                        completed_at,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO game_players
                    (game_id, user_id, display_name, role, team, won, survived)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        game_id,
                        user_id,
                        display_name(player.get("name", "플레이어")),
                        role,
                        team,
                        won,
                        survived,
                    ),
                )
            conn.execute("DELETE FROM room_snapshots WHERE room_code = ?", (room.room_code,))
        return game_id

    def profile(self, user_id: int) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

    def leaderboard(self, limit: int = 10) -> List[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT display_name, games, wins, losses, points,
                       CASE WHEN games = 0 THEN 0.0 ELSE 100.0 * wins / games END AS win_rate
                FROM users
                WHERE games > 0
                ORDER BY points DESC, wins DESC, games ASC, display_name COLLATE NOCASE ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def recent_games(self, limit: int = 5) -> List[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT room_code, winner, day_count, completed_at, highlights
                FROM game_history
                ORDER BY game_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()


def get_current_room(rooms: Dict[str, Any], user_id: int) -> Optional[Any]:
    for room in rooms.values():
        if user_id in room.players:
            return room
    return None


def resolve_room(rooms: Dict[str, Any], user_id: int, args: Iterable[str]) -> Optional[Any]:
    values = list(args)
    if values:
        return rooms.get(values[0].upper())
    return get_current_room(rooms, user_id)


def is_host(room: Any, user_id: int) -> bool:
    return room is not None and room.host_id == user_id


def player_status(player: Dict[str, Any], reveal_roles: bool = False) -> str:
    label = "🤖" if player.get("is_bot") else "👤"
    alive = "생존" if player.get("alive") else "사망"
    role = f" · {player.get('role') or '미배정'}" if reveal_roles else ""
    return f"{label} {display_name(player.get('name', '플레이어'))} · {alive}{role}"


def register_v3_handlers(
    app: Application,
    rooms: Dict[str, Any],
    store: MafiaStore,
    add_bot_to_room: Callable[[Any], Optional[Dict[str, Any]]],
) -> None:
    """기존 핸들러와 충돌하지 않는 v3 명령어를 등록한다."""

    async def v3_help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            "🎭 **Mafia Bot v3 명령어**\n\n"
            "`/room [방코드]` 현재 로비와 진행 상태\n"
            "`/rooms` 입장 가능한 공개 로비\n"
            "`/profile` 내 영구 전적\n"
            "`/rank` 포인트 랭킹\n"
            "`/history [개수]` 최근 완료 게임\n"
            "`/roles [직업]` 역할 설명\n"
            "`/leave` 대기 로비에서 퇴장\n"
            "`/kick 방코드 사용자ID` 방장이 대기 로비 참가자 퇴장\n"
            "`/add_bots 방코드 수` 방장이 AI를 일괄 추가\n"
            "`/settings 방코드 events on|off` 랜덤 사건 설정\n"
            "`/rematch 방코드` 종료 게임을 새 라운드 대기로 초기화\n\n"
            "기존 명령어 `/create`, `/join`, `/fast`, `/start_game`도 그대로 사용할 수 있습니다."
        )
        await update.effective_message.reply_text(text, parse_mode="Markdown")

    async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        store.touch_user(user.id, user.first_name)
        row = store.profile(user.id)
        if row is None or row["games"] == 0:
            await update.effective_message.reply_text(
                "📊 **아직 완료한 게임이 없습니다.**\n첫 게임을 끝내면 영구 전적과 포인트가 기록됩니다.",
                parse_mode="Markdown",
            )
            return
        rate = row["wins"] / row["games"] * 100
        await update.effective_message.reply_text(
            "📊 **내 플레이어 프로필**\n\n"
            f"이름: **{display_name(row['display_name'])}**\n"
            f"포인트: **{row['points']} P**\n"
            f"완료 게임: **{row['games']}**\n"
            f"승리 / 패배: **{row['wins']} / {row['losses']}**\n"
            f"승률: **{rate:.1f}%**\n"
            f"마피아 승리: **{row['mafia_wins']}**\n"
            f"시민 진영 승리: **{row['citizen_wins']}**",
            parse_mode="Markdown",
        )

    async def rank_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        rows = store.leaderboard(10)
        if not rows:
            await update.effective_message.reply_text("🏆 아직 완료된 게임 전적이 없습니다.")
            return
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for index, row in enumerate(rows, start=1):
            mark = medals[index - 1] if index <= 3 else f"{index}."
            lines.append(
                f"{mark} **{display_name(row['display_name'])}** — {row['points']}P "
                f"· {row['wins']}승 {row['losses']}패 · {row['win_rate']:.1f}%"
            )
        await update.effective_message.reply_text(
            "🏆 **포인트 랭킹 TOP 10**\n\n" + "\n".join(lines),
            parse_mode="Markdown",
        )

    async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        count = 5
        if context.args:
            try:
                count = max(1, min(10, int(context.args[0])))
            except ValueError:
                await update.effective_message.reply_text("사용법: `/history [1~10]`", parse_mode="Markdown")
                return
        rows = store.recent_games(count)
        if not rows:
            await update.effective_message.reply_text("📜 아직 저장된 완료 게임이 없습니다.")
            return
        lines = []
        for row in rows:
            highlights = json.loads(row["highlights"])
            suffix = f" · {display_name(highlights[-1], '')}" if highlights else ""
            lines.append(
                f"• `{row['room_code']}` — **{row['winner']} 승리** · {row['day_count']}일차 · "
                f"{row['completed_at']}{suffix}"
            )
        await update.effective_message.reply_text(
            "📜 **최근 완료 게임**\n\n" + "\n".join(lines),
            parse_mode="Markdown",
        )

    async def room_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        room = resolve_room(rooms, user.id, context.args)
        if room is None:
            await update.effective_message.reply_text("❌ 조회할 방이 없습니다. `/room 방코드`로 다시 시도하세요.", parse_mode="Markdown")
            return
        is_member = user.id in room.players
        reveal = is_host(room, user.id) and room.state == "GAME_OVER"
        members = "\n".join(
            f"• {player_status(player, reveal)}"
            for player in room.players.values()
        )
        host = room.players.get(room.host_id, {}).get("name", "알 수 없음")
        visibility = "비공개" if room.password else "공개"
        settings = getattr(room, "settings", {})
        event_status = "켜짐" if settings.get("random_events", True) else "꺼짐"
        member_note = "" if is_member else "\n\n이 방에 입장하려면 `/join 방코드`를 사용하세요."
        await update.effective_message.reply_text(
            "🎮 **방 정보**\n\n"
            f"방 코드: `{room.room_code}` · {visibility}\n"
            f"방장: **{display_name(host)}**\n"
            f"상태: **{STATE_LABELS.get(room.state, room.state)}**\n"
            f"인원: **{len(room.players)}/16** · 마피아 설정: **{room.target_mafia_count}명**\n"
            f"랜덤 사건: **{event_status}**\n\n"
            f"**참가자**\n{members}{member_note}",
            parse_mode="Markdown",
        )

    async def rooms_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        waiting = [room for room in rooms.values() if room.state == "WAITING"]
        if not waiting:
            await update.effective_message.reply_text("📭 현재 입장 가능한 대기 로비가 없습니다. `/create`로 새 방을 만들 수 있습니다.", parse_mode="Markdown")
            return
        lines = []
        for room in sorted(waiting, key=lambda value: value.room_code)[:12]:
            host = room.players.get(room.host_id, {}).get("name", "방장")
            lock = "🔒" if room.password else "🔓"
            lines.append(
                f"• `{room.room_code}` {lock} · {len(room.players)}/16명 · 방장 {display_name(host)}"
            )
        await update.effective_message.reply_text(
            "🚪 **입장 가능한 로비**\n\n" + "\n".join(lines) + "\n\n입장: `/join 방코드 [비밀번호]`",
            parse_mode="Markdown",
        )

    async def roles_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if context.args:
            role = " ".join(context.args).strip()
            guide = ROLE_GUIDE.get(role)
            if guide is None:
                valid = ", ".join(ROLE_GUIDE)
                await update.effective_message.reply_text(f"❌ 알 수 없는 직업입니다.\n사용 가능 직업: {valid}")
                return
            await update.effective_message.reply_text(
                f"🎭 **{role}**\n\n{guide}", parse_mode="Markdown"
            )
            return
        lines = [f"• **{role}** — {guide}" for role, guide in ROLE_GUIDE.items()]
        await update.effective_message.reply_text(
            "🎭 **직업 안내**\n\n" + "\n".join(lines) + "\n\n상세 보기: `/roles 직업명`",
            parse_mode="Markdown",
        )

    async def leave_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        room = get_current_room(rooms, user.id)
        if room is None:
            await update.effective_message.reply_text("❌ 현재 참여 중인 방이 없습니다.")
            return
        if room.state not in {"WAITING", "GAME_OVER"}:
            await update.effective_message.reply_text("❌ 게임 진행 중에는 퇴장할 수 없습니다. 게임 종료 후 `/rematch` 또는 새 방을 이용하세요.")
            return
        name = display_name(room.players[user.id].get("name", user.first_name))
        del room.players[user.id]
        if not room.players:
            rooms.pop(room.room_code, None)
            store.remove_room_snapshot(room.room_code)
            await update.effective_message.reply_text("✅ 방에서 나왔습니다. 마지막 참가자여서 방도 정리했습니다.")
            return
        if room.host_id == user.id:
            room.host_id = next(iter(room.players))
            notice = f" 방장 권한은 **{display_name(room.players[room.host_id]['name'])}**에게 전달되었습니다."
        else:
            notice = ""
        store.save_room_snapshot(room)
        await update.effective_message.reply_text(f"✅ **{name}** 님이 방에서 나왔습니다.{notice}", parse_mode="Markdown")

    async def kick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if len(context.args) != 2:
            await update.effective_message.reply_text("사용법: `/kick 방코드 사용자ID`", parse_mode="Markdown")
            return
        room = rooms.get(context.args[0].upper())
        if not is_host(room, user.id):
            await update.effective_message.reply_text("❌ 방장만 사용할 수 있습니다.")
            return
        if room.state != "WAITING":
            await update.effective_message.reply_text("❌ 대기 로비에서만 참가자를 내보낼 수 있습니다.")
            return
        try:
            target_id = int(context.args[1])
        except ValueError:
            await update.effective_message.reply_text("❌ 사용자ID는 숫자여야 합니다.")
            return
        if target_id == user.id or target_id not in room.players:
            await update.effective_message.reply_text("❌ 유효한 대상이 아닙니다.")
            return
        target = room.players.pop(target_id)
        store.save_room_snapshot(room)
        await update.effective_message.reply_text(f"✅ **{display_name(target['name'])}** 님을 로비에서 내보냈습니다.", parse_mode="Markdown")

    async def add_bots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if len(context.args) != 2:
            await update.effective_message.reply_text("사용법: `/add_bots 방코드 1~12`", parse_mode="Markdown")
            return
        room = rooms.get(context.args[0].upper())
        if not is_host(room, user.id):
            await update.effective_message.reply_text("❌ 방장만 AI를 추가할 수 있습니다.")
            return
        if room.state != "WAITING":
            await update.effective_message.reply_text("❌ AI 추가는 대기 로비에서만 가능합니다.")
            return
        try:
            requested = max(1, min(12, int(context.args[1])))
        except ValueError:
            await update.effective_message.reply_text("❌ AI 수는 1~12 사이의 숫자여야 합니다.")
            return
        added = 0
        for _ in range(requested):
            if len(room.players) >= 16 or add_bot_to_room(room) is None:
                break
            added += 1
        store.save_room_snapshot(room)
        await update.effective_message.reply_text(
            f"🤖 AI **{added}명**을 추가했습니다. 현재 **{len(room.players)}/16명**입니다.",
            parse_mode="Markdown",
        )

    async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if len(context.args) != 3 or context.args[1].lower() != "events":
            await update.effective_message.reply_text(
                "사용법: `/settings 방코드 events on|off`\n방장만 설정할 수 있습니다.",
                parse_mode="Markdown",
            )
            return
        room = rooms.get(context.args[0].upper())
        if not is_host(room, user.id):
            await update.effective_message.reply_text("❌ 방장만 설정할 수 있습니다.")
            return
        if room.state != "WAITING":
            await update.effective_message.reply_text("❌ 공정성을 위해 대기 로비에서만 설정을 변경할 수 있습니다.")
            return
        value = context.args[2].lower()
        if value not in {"on", "off"}:
            await update.effective_message.reply_text("❌ 값은 `on` 또는 `off`만 사용할 수 있습니다.", parse_mode="Markdown")
            return
        room.settings["random_events"] = value == "on"
        store.save_room_snapshot(room)
        state = "활성화" if value == "on" else "비활성화"
        await update.effective_message.reply_text(f"⚙️ 랜덤 사건을 **{state}**했습니다.", parse_mode="Markdown")

    async def rematch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        room = resolve_room(rooms, user.id, context.args)
        if room is None:
            await update.effective_message.reply_text("❌ 방을 찾을 수 없습니다. 사용법: `/rematch 방코드`", parse_mode="Markdown")
            return
        if not is_host(room, user.id):
            await update.effective_message.reply_text("❌ 방장만 재대결 로비를 열 수 있습니다.")
            return
        if room.state != "GAME_OVER":
            await update.effective_message.reply_text("❌ 종료된 게임에서만 재대결을 시작할 수 있습니다.")
            return
        for player in room.players.values():
            player["alive"] = True
            player["role"] = None
            if player.get("personality"):
                player["personality"]["trust"] = {}
                player["personality"]["anger"] = {}
        room.state = "WAITING"
        room.day = 0
        room.public_known_roles.clear()
        room.self_proclaimed_mafias.clear()
        room.previous_votes.clear()
        room.event_suspicion.clear()
        room.highlights.clear()
        room.last_dead = None
        room.last_executed = None
        room.medium_memory = None
        room.current_event = None
        room.silenced_target = None
        room.defense_target = None
        room.votes = {}
        room.final_votes = {}
        room.reset_night()
        store.save_room_snapshot(room)
        await update.effective_message.reply_text(
            f"🔁 **재대결 로비를 열었습니다.**\n방 코드: `{room.room_code}`\n인원: {len(room.players)}/16명\n\n준비되면 `/start_game {room.room_code}`를 사용하세요.",
            parse_mode="Markdown",
        )

    app.add_handler(CommandHandler("v3help", v3_help_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("rank", rank_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("room", room_cmd))
    app.add_handler(CommandHandler("rooms", rooms_cmd))
    app.add_handler(CommandHandler("roles", roles_cmd))
    app.add_handler(CommandHandler("leave", leave_cmd))
    app.add_handler(CommandHandler("kick", kick_cmd))
    app.add_handler(CommandHandler("add_bots", add_bots_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("rematch", rematch_cmd))
