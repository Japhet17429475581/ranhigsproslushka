import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

HOMOGLYPHS = str.maketrans("aceopxykmtbhіјѕ", "асеорхукмтвніјѕ")
LEET = str.maketrans({"0": "о", "3": "з", "6": "б", "@": "а", "$": "с"})
LINK = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>]+|\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s<>]*)?")
PHONE = re.compile(r"(?<!\w)(?:\+?\d[\s().-]*){10,15}(?!\w)")
EMAIL = re.compile(r"[\w.+-]+\s*@\s*[\w-]+(?:\s*\.\s*[\w-]+)+", re.UNICODE)
MENTION = re.compile(r"(?<!\w)@[a-zA-Z0-9_]{3,32}\b")
NAME = re.compile(r"\b[А-ЯЁ][а-яё]{2,}[- ]+[А-ЯЁ][а-яё]{2,}(?:\s+[А-ЯЁ][а-яё]{2,})?\b")
ACCUSATION = re.compile(r"\b(?:вор(?:ует|ов|ишка)?|укра[лд]\w*|взят\w*|мошенн\w*|преступ\w*|наркоторг\w*|педофил\w*)\b")


def canonical_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return "".join(
        char
        for char in text
        if unicodedata.category(char) not in {"Cf", "Mn", "Me", "Cs"}
        and (unicodedata.category(char) != "Cc" or char in "\n\t")
    )


def normalize_text(text: str) -> str:
    text = canonical_text(text).translate(HOMOGLYPHS)
    return re.sub(r"([^\W\d_])\1{2,}", r"\1", text)


def fingerprint(text: str) -> str:
    compact = re.sub(r"[\W_]+", "", normalize_text(text))
    return hashlib.sha256(compact.encode()).hexdigest()


@dataclass(frozen=True)
class ModerationResult:
    status: str
    reasons: tuple[str, ...] = ()


class Moderator:
    def __init__(self, path: str | Path):
        rules = json.loads(Path(path).read_text(encoding="utf-8"))
        self.allowed_hosts = {host.lower() for host in rules["allowed_link_hosts"]}
        self.exceptions = {normalize_text(word) for word in rules["exceptions"]}
        self.categories = []
        for category in rules["categories"]:
            if category["severity"] not in {"review", "blocked"}:
                raise ValueError("Неизвестный severity в moderation_rules.json")
            words = []
            for word in category["words"]:
                # Allow separators/repeated letters inside a stem, retain a word boundary
                # before it. Exceptions are masked before matching.
                normalized = normalize_text(word)
                letters = [re.escape(char) + "+" for char in normalized if char.isalnum()]
                words.append(re.compile(r"(?<!\w)" + r"[\W_]*".join(letters), re.UNICODE))
            patterns = [re.compile(pattern, re.I | re.S) for pattern in category["patterns"]]
            self.categories.append((category["label"], category["severity"], words, patterns))

    def check(self, text: str, entity_risk: bool = False) -> ModerationResult:
        canonical = canonical_text(text)
        normalized = normalize_text(text)

        def mask_exceptions(view: str) -> str:
            return re.sub(
                r"\w+",
                lambda match: " " * len(match[0]) if normalize_text(match[0]) in self.exceptions else match[0],
                view,
            )

        masked = mask_exceptions(normalized)
        canonical_masked = mask_exceptions(canonical)
        leet = masked.translate(LEET)
        reasons: list[str] = []
        blocked = False
        for label, severity, words, patterns in self.categories:
            if any(pattern.search(view) for pattern in words + patterns for view in (masked, leet, canonical_masked)):
                reasons.append(label)
                blocked |= severity == "blocked"

        if PHONE.search(canonical) or EMAIL.search(canonical) or MENTION.search(canonical):
            reasons.append("Контактные данные или личный профиль")
        if entity_risk:
            reasons.append("Скрытая ссылка или упоминание профиля")
        if NAME.search(text) and ACCUSATION.search(canonical):
            reasons.append("Адресные обвинения в отношении человека")
            blocked = True
        elif ACCUSATION.search(canonical):
            reasons.append("Возможные обвинения — требуется оценка контекста")
        if NAME.search(text):
            reasons.append("Возможное полное имя человека")
        if re.search(r"\b\d{4}\s+\d{6}\b|\b\d{3}[- ]\d{3}[- ]\d{3}[- ]\d{2}\b", canonical):
            reasons.append("Возможный персональный идентификатор")
        links = LINK.findall(canonical)
        if any(not self._trusted_link(link) for link in links):
            reasons.append("Непроверенная, личная или рекламная ссылка")
        if re.search(r"(?i)(?:tg:|t\s*\.\s*me\s*/|vk\s*\.\s*com\s*/|https?\s*[:：]\s*[/\\])", canonical) and not links:
            reasons.append("Замаскированная ссылка")
        words_in_text = normalized.split()
        if len(words_in_text) >= 8 and len(set(words_in_text)) / len(words_in_text) < 0.3:
            reasons.append("Многократное повторение текста")
        if re.search(r"([^\W\d_])\1{7,}", canonical):
            reasons.append("Многократное повторение символов")
        # A script we cannot check must never be implicitly considered safe.
        if any(char.isalpha() and not ("а" <= char <= "я" or "a" <= char <= "z") for char in canonical):
            reasons.append("Символы вне поддерживаемых алфавитов")
        reasons = list(dict.fromkeys(reasons))
        return ModerationResult("blocked" if blocked else "review" if reasons else "safe", tuple(reasons))

    def _trusted_link(self, value: str) -> bool:
        try:
            parsed = urlsplit(value if "://" in value else "https://" + value)
            if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}:
                return False
            host = (parsed.hostname or "").rstrip(".")
            # Exact hosts only: subdomains must be explicitly approved.
            if host not in self.allowed_hosts:
                return False
            keys = {key.casefold() for key, _ in parse_qsl(parsed.query)}
            return not keys.intersection(
                {"ref", "referrer", "referral", "start", "url", "redirect", "next", "return", "target"}
            )
        except ValueError:
            return False
