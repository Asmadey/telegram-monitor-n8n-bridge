"""Стоп-слова: отсев постов до обращения к модели (задача 11.3).

Решение владельца: «я не хочу получать вакансии для разработчиков» —
слово «разработчик» в стоп-список, и такие посты до LLM не доходят вовсе.
Токены не жгутся, а модели не приходится исключать то, что отсекается
строкой.

Совпадение — подстрока без учёта регистра, и это осознанный компромисс:
«разработчик» отсечёт «разработчица» (обычно то, что нужно), но отсечёт и
пост, где слово попалось случайно. Поэтому отсеянное ВСЕГДА считается и
показывается: без счётчика слишком широкое стоп-слово выглядит как «канал
замолчал», а это худший вид отказа — необъяснимая тишина.

Ищем только в тексте поста. Ссылка несёт имя канала, и стоп-слово,
совпавшее с ним, вырезало бы канал целиком.
"""

# Потолок не придирка: каждое слово — проход по каждому посту, а посты
# приходят пачками по полсотни с каждого канала источника.
MAX_STOP_WORDS = 100


def parse_stop_words(raw: str) -> list[str]:
    """Список из текстового поля: по слову на строку, без повторов."""
    words: list[str] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        word = line.strip().lower()
        if not word or word in seen:
            continue
        seen.add(word)
        words.append(word)
        if len(words) >= MAX_STOP_WORDS:
            break
    return words


def split_by_stop_words(
    messages: list[dict], words: list[str]
) -> tuple[list[dict], list[dict]]:
    """Разделить посты на «идут в модель» и «отсеяны».

    Возвращается ОБЕ части, а не только первая: отсеянные нужны, чтобы их
    посчитать и пометить обработанными. Молча потерять их нельзя.
    """
    if not words:
        return list(messages), []

    kept: list[dict] = []
    dropped: list[dict] = []
    for message in messages:
        text = (message.get("text") or "").lower()
        if text and any(word in text for word in words):
            dropped.append(message)
        else:
            kept.append(message)
    return kept, dropped
