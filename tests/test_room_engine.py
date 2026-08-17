import asyncio
import threading
import unittest

from mafia_bot import (
    MafiaRoom,
    add_ai_bot_to_room,
    ensure_event_loop,
    generate_room_code,
    rooms,
)


class MafiaRoomEngineTests(unittest.TestCase):
    def setUp(self):
        rooms.clear()

    def tearDown(self):
        rooms.clear()

    def test_room_initializes_v3_safe_defaults(self):
        room = MafiaRoom("ROOM1", 1)
        self.assertEqual(room.settings, {"random_events": True})
        self.assertIsNone(room.silenced_target)
        self.assertEqual(room.state, "WAITING")

    def test_add_ai_bot_respects_room_capacity(self):
        room = MafiaRoom("ROOM1", 1)
        room.add_player(1, "Host")
        bot = add_ai_bot_to_room(room)
        self.assertIsNotNone(bot)
        self.assertTrue(bot["is_bot"])
        self.assertEqual(len(room.players), 2)

        while len(room.players) < 16:
            self.assertIsNotNone(add_ai_bot_to_room(room))
        self.assertIsNone(add_ai_bot_to_room(room))
        self.assertEqual(len(room.players), 16)

    def test_role_assignment_has_mafia_and_unique_players(self):
        room = MafiaRoom("ROOM1", 1)
        for user_id in range(1, 7):
            room.add_player(user_id, f"User{user_id}")
        room.target_mafia_count = 2
        room.assign_roles()
        roles = [player["role"] for player in room.players.values()]
        self.assertEqual(len(roles), 6)
        self.assertEqual(roles.count("마피아"), 2)
        self.assertNotIn(None, roles)

    def test_room_code_is_not_reused_when_registered(self):
        rooms["ABCDE"] = MafiaRoom("ABCDE", 1)
        for _ in range(30):
            self.assertNotEqual(generate_room_code(), "ABCDE")

    def test_ensure_event_loop_creates_loop_in_thread_without_default(self):
        result = {}

        def worker():
            loop = ensure_event_loop()
            result["is_running"] = loop.is_running()
            result["is_current"] = asyncio.get_event_loop() is loop
            loop.close()
            asyncio.set_event_loop(None)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        self.assertFalse(result["is_running"])
        self.assertTrue(result["is_current"])


if __name__ == "__main__":
    unittest.main()
