import unittest

from bot import is_search, refresh_message_id


class CommandTests(unittest.TestCase):
    def test_search_and_group_mention(self):
        for text in ("/search", "/search@OurBot", "/search@ourbot"):
            self.assertTrue(is_search({"message": {"chat": {"id": -123}, "text": text}}, "-123", "OurBot"))

    def test_other_chats_and_commands_are_ignored(self):
        for chat, text in ((456, "/search"), (-123, "/search@AnotherBot"), (-123, "search"), (-123, "/start")):
            self.assertFalse(is_search({"message": {"chat": {"id": chat}, "text": text}}, "-123", "OurBot"))
        self.assertFalse(is_search({}, "-123", "OurBot"))

    def test_refresh_requires_configured_chat(self):
        update = {"callback_query": {"data": "refresh_prices", "message": {"chat": {"id": -123}, "message_id": 42}}}
        self.assertEqual(refresh_message_id(update, "-123"), 42)
        self.assertIsNone(refresh_message_id(update, "999"))
        self.assertIsNone(refresh_message_id({}, "-123"))
