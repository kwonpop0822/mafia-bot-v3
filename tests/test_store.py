import tempfile
import unittest
from pathlib import Path

from mafia_v3 import MafiaStore


class FakeRoom:
    def __init__(self):
        self.room_code = "TEST1"
        self.day = 3
        self.highlights = ["첫날 밤 사건", "테스트 유저가 승리"]
        self.state = "GAME_OVER"
        self.host_id = 1001
        self.target_mafia_count = 1
        self.settings = {"random_events": True}
        self.players = {
            1001: {
                "name": "Alice",
                "role": "마피아",
                "alive": True,
                "is_bot": False,
            },
            1002: {
                "name": "Bob",
                "role": "경찰",
                "alive": False,
                "is_bot": False,
            },
            -100001: {
                "name": "Bot_001",
                "role": "시민",
                "alive": True,
                "is_bot": True,
            },
        }


class MafiaStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MafiaStore(Path(self.temp_dir.name) / "test.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_touch_user_creates_profile(self):
        self.store.touch_user(1001, "Alice")
        profile = self.store.profile(1001)
        self.assertIsNotNone(profile)
        self.assertEqual(profile["display_name"], "Alice")
        self.assertEqual(profile["games"], 0)

    def test_record_game_updates_human_profiles_and_excludes_bots(self):
        room = FakeRoom()
        game_id = self.store.record_game(room, "마피아")

        self.assertEqual(game_id, 1)
        winner = self.store.profile(1001)
        loser = self.store.profile(1002)
        self.assertEqual(winner["games"], 1)
        self.assertEqual(winner["wins"], 1)
        self.assertEqual(winner["mafia_wins"], 1)
        self.assertEqual(winner["points"], 17)  # 승리 15점 + 생존 2점
        self.assertEqual(loser["losses"], 1)
        self.assertEqual(loser["points"], 4)
        self.assertIsNone(self.store.profile(-100001))

    def test_leaderboard_orders_by_points_then_wins(self):
        room = FakeRoom()
        self.store.record_game(room, "마피아")
        self.store.touch_user(1003, "Carol")
        ranking = self.store.leaderboard()
        self.assertEqual(ranking[0]["display_name"], "Alice")
        self.assertEqual(ranking[0]["points"], 17)
        self.assertEqual(ranking[1]["display_name"], "Bob")

    def test_snapshot_is_replaced_for_same_room(self):
        room = FakeRoom()
        self.store.save_room_snapshot(room)
        room.day = 4
        self.store.save_room_snapshot(room)
        self.store.record_game(room, "마피아")
        self.assertEqual(len(self.store.recent_games()), 1)


if __name__ == "__main__":
    unittest.main()
