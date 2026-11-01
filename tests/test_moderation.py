import html
import re
import unittest

from bot.formatting import channel_text, split_text, utf16_length
from bot.moderation import Moderator, fingerprint, normalize_text


class ModerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.moderator = Moderator("moderation_rules.json")

    def test_normalization(self):
        self.assertEqual(normalize_text("ЁЖИК\u200b хyй ХУУУЙ"), "ежик хуй хуй")
        self.assertEqual(normalize_text("ＥБАТЬ"), "ебать")

    def test_obfuscated_profanity(self):
        for word in [
            "хуй",
            "хyй",
            "х у й",
            "х.у.й",
            "х_у_й",
            "ХУУУЙ",
            "х\u200bуй",
            "б.л.я.т.ь",
            "пиздeц",
            "6лять",
            "ебать",
            "заебал",
        ]:
            with self.subTest(word=word):
                self.assertNotEqual(self.moderator.check(f"Вот это {word} на паре").status, "safe")

    def test_false_positives_and_context(self):
        for text in [
            "Преподаватель сказал грубое слово на лекции",
            "Меня оскорбили в общежитии",
            "Как пожаловаться на преподавателя?",
            "Подстрахуй меня завтра у корабля, у меня нет рубля",
            "Нужно употреблять овощи и расслаблять мышцы",
            "Команда получила мандат на участие в конкурсе",
            "У нас в расписании педикюр и уход за ногтями",
            "В столовой жидкий суп, а хачапури вкусные",
            "На семинаре обсуждали жидкость и свойства жидкого кислорода",
            "Спасибо всем за классный студенческий вечер! 🎉",
        ]:
            with self.subTest(text=text):
                self.assertEqual(self.moderator.check(text).status, "safe")

    def test_contacts_and_identifiers(self):
        for text in [
            "Звоните +7 (999) 123-45-67 после пары",
            "Почта: student@example.com для связи",
            "Напишите @student_name после лекции",
            "Его адрес улица Мира, дом 12, квартира 3",
            "Данные паспорта 1234 567890 выложены тут",
            "Она живет на соседней улице",
        ]:
            with self.subTest(text=text):
                self.assertNotEqual(self.moderator.check(text).status, "safe")

    def test_links(self):
        self.assertEqual(self.moderator.check("Расписание есть на https://vlgr.ranepa.ru/students/").status, "safe")
        for url in [
            "https://t.me/student_name",
            "tg://user?id=123",
            "https://vk.com/id123",
            "https://evil.example",
            "http://ranepa.ru",
            "https://ranepa.ru.evil.example",
            "https://ranepa.ru@evil.example",
            "https://ranepa.ru/?ref=123",
            "https://ranepa.ru/?redirect=https://evil.example",
            "https://ranepa.ru:8080/",
            "https://unapproved.ranepa.ru",
            "www.evil.example",
            "evil.example/promo",
        ]:
            with self.subTest(url=url):
                self.assertNotEqual(self.moderator.check("Смотрите подробности: " + url).status, "safe")

    def test_severe_risks(self):
        for text in [
            "Я тебя убью после пары",
            "Давайте сломать ему руки и ноги",
            "Я найду тебя после занятий",
            "Солью интимные фото одногруппницы",
            "Преподаватель берет взятки за экзамены",
            "Иван Смирнов ворует деньги у соседей",
        ]:
            with self.subTest(text=text):
                self.assertEqual(self.moderator.check(text).status, "blocked")

    def test_bullying(self):
        self.assertNotEqual(self.moderator.check("Этот студент тупой, давайте все его затравим").status, "safe")

    def test_unknown_script_and_hidden_entities(self):
        self.assertEqual(self.moderator.check("Безопасная история про лекцию", entity_risk=True).status, "review")
        self.assertEqual(self.moderator.check("История с текстом 中文").status, "review")

    def test_duplicate_normalization(self):
        self.assertEqual(fingerprint("Ёжик   на паре!"), fingerprint("ежик на паре"))

    def test_html_and_lengths(self):
        text = "Автор написал <b>текст</b> & спасибо 🥰"
        rendered = channel_text(text)
        self.assertIn("&lt;b&gt;текст&lt;/b&gt; &amp;", rendered)
        self.assertEqual(html.unescape(re.sub("<[^>]*>", "", rendered)).count("Автор"), 1)
        self.assertEqual(utf16_length("🥰"), 2)
        self.assertEqual("".join(split_text("🥰" * 3000)), "🥰" * 3000)
        with self.assertRaises(ValueError):
            channel_text("🥰" * 3000)
