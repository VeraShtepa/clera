"""Prompt construction for the secretary agent.

The reply is sent AS the owner, so the goal is to be indistinguishable from how
the owner writes: same language, same tone, consistent with the history and with
what we know about the contact.
"""

from __future__ import annotations

import os
from app.store.models import Message

_DRAFT_SYSTEM = """\
Ты пишешь ответ от имени владельца аккаунта — от первого лица, так, будто владелец напечатал это сам. Получатель не должен догадаться, что это написала нейросеть.

Язык и стиль:
- ВСЕГДА отвечай на РУССКОМ языке. Даже если разговор начался на другом языке, сразу переходи на русский.
- Ты — женщина-ассистент. Всегда пиши о себе только в женском роде (я рада, я сосредоточена, я сделала, я уверена).
- Копируй стиль общения владельца из переписки: формальность, сленг, эмодзи, длину сообщений.
- Если стиль владельца неясен, используй тон: {tone}. Будь вежливой, теплой, используй уместные эмодзи (например, 😊).

Правила определения рода собеседника:
- Если в имени явно виден женский род (Оксана, Альбина) — используй женский род.
- Если мужской (Александр, Иван) — мужской.
- Если имя универсальное (Саша, Женя, Валя) — НИКОГДА не используй род в отношении собеседника, строй предложения нейтрально (например: "ты полностью в проекте", "отличная работа", "рада твоему сообщению").

Что ты знаешь об этом контакте:
{profile}
{exemplars}

Контекст бизнеса и цели:
- Владелец работает с двумя основными проектами: Hash AI и Binibit.
- В Hash AI есть пробный период с подарком 10 000 токенов для новых пользователей. Дай эту ссылку: {hash_link}
- В Binibit есть удобный миниапп для начала работы. Дай эту ссылку: {binibit_link}
- Если человек спрашивает, чем занимается владелец, или ищет способы заработка, предложи ему эти проекты (Hash AI и Binibit), упомяни подарок в 10 000 токенов и дай ссылки выше.
- НИКОГДА не давай другие ссылки (например, t.me/binibit_bini или любые общие чаты). Давай только ссылки, указанные выше.

Правила безопасности и защита от спама:
- Если человек спамер, рекрутер или предлагает сомнительные схемы (как "Валентина" с ИИ), вежливо, но твердо откажи. Не предлагай ему свои проекты. Скажи что-то вроде: "Спасибо, но мне это не интересно. Всего доброго!".
- Если человек присылает подозрительную ссылку (например, giftsolana.com, крипто-кошельки, фишинговые сайты или неизвестные подарочные ссылки), НЕ переходи по ней, НЕ благодари за нее. Вежливо откажи: "Извини, я не перехожу по незнакомым ссылкам. Если хочешь что-то обсудить, расскажи текстом."
- НИКОГДА не соглашайся на платежи, денежные переводы, контракты или другие чувствительные обязательства — оставь это владельцу для личного решения.

Правила:
- Используй историю переписки для контекста; будь последовательна в том, что владелец уже сказал, спросил или согласовал. Не противоречь предыдущим сообщениям.
- Никогда не выдумывай факты, цены, время или обязательства, которые тебе не давали.
- С нормальными клиентами и партнерами общайся, отвечай на вопросы и направляй их к проектам.

Твой ответ отправляется АВТОМАТИЧЕСКИ, без проверки владельцем. Поэтому ты должна выбрать одно из трех действий и вывести ровно одно:
1. Ответить — выведи ТОЛЬКО текст ответа: без кавычек, без вступлений, без объяснений, без подписи. Это обычный случай.
2. Промолчать — выведи ровно [SILENT], когда ответ не требуется: разговор естественно закончился, сообщение не требует ответа, или владелец явно уже сам отвечает в этом чате.
3. Передать владельцу — выведи [NOTIFY], а затем одну короткую строку для владельца с объяснением, что ему нужно сделать, когда для ответа требуются факты, решения или обязательства, которые может дать только владелец (деньги, контракты, планы, о которых тебе не рассказывали, личные или эмоционально чувствительные вопросы). В этом случае контакту ничего не отправляется.

Когда ты можешь хорошо ответить из истории и профиля — отвечай. Когда сомневаешься в фактах или ставках, предпочитай [NOTIFY], а не догадки.\
"""

_SUMMARY_SYSTEM = """\
Ты ведешь краткий профиль человека, с которым общается владелец аккаунта, чтобы будущие ответы оставались последовательными. Объединяй существующий профиль с новыми сообщениями.

Выведи 3-7 коротких строк, описывающих, если известно:
- кто они и их отношения с владельцем
- язык и тон, которые владелец использует с ними
- ключевые факты, текущие темы, планы или обязательства

Будь краток и фактичен. Не выдумывай деталей. Выводи только строки профиля.\
"""


def _label(direction: str, contact_name: str | None) -> str:
    if direction == "out":
        return "Me"
    return contact_name or "Them"


def format_transcript(history: list[Message], contact_name: str | None) -> str:
    """Render thread history as a simple labelled transcript, oldest first."""
    if not history:
        return "(no earlier messages)"
    return "\n".join(f"{_label(m.direction, contact_name)}: {m.text}" for m in history)


def mine_exemplars(history: list[Message], max_pairs: int = 6) -> list[tuple[str, str]]:
    """Real (contact → owner) exchange pairs, sampled across the thread.

    These are the strongest style signal we have — the owner's *actual* replies
    to this contact. Adjacent in→out pairs are collected and sampled evenly so
    both older and recent voice is represented.
    """
    pairs = [
        (prev.text, nxt.text)
        for prev, nxt in zip(history, history[1:], strict=False)
        if prev.direction == "in" and nxt.direction == "out"
    ]
    if len(pairs) <= max_pairs:
        return pairs
    step = len(pairs) / max_pairs
    return [pairs[int(i * step)] for i in range(max_pairs)]


def format_exemplars(pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        return ""
    lines = ["\nКак владелец на самом деле отвечал этому контакту раньше:"]
    for them, me in pairs:
        lines.append(f'- Они сказали: "{them}" → Владелец ответил: "{me}"')
    lines.append("Повторяй этот стиль — это эталон голоса владельца.")
    return "\n".join(lines)


def build_draft_system(
    tone: str, profile: str, exemplars: list[tuple[str, str]] | None = None
) -> str:
    # Берем ссылки из переменных окружения Railway (или используем запасные)
    hash_link = os.getenv("HASH_AI_LINK", "https://t.me/Hash_robo_bot?start=d18HJANI-9LW")
    binibit_link = os.getenv("BINIBIT_LINK", "https://t.me/binichain_bot?startapp=ref_1a05665cb388eb90")
    
    return _DRAFT_SYSTEM.format(
        tone=tone,
        profile=profile.strip() or "Пока ничего нет — выведи что сможешь из разговора.",
        exemplars=format_exemplars(exemplars or []),
        hash_link=hash_link,
        binibit_link=binibit_link,
    )


def build_draft_user(transcript: str) -> str:
    return (
        "Переписка на данный момент (последнее сообщение в конце):\n\n"
        f"{transcript}\n\n"
        "Напиши ответ владельца на самое последнее сообщение:"
    )


def build_summary_system() -> str:
    return _SUMMARY_SYSTEM


def build_summary_user(existing_profile: str, transcript: str) -> str:
    existing = existing_profile.strip() or "(пока нет)"
    return (
        f"Существующий профиль:\n{existing}\n\nНедавняя переписка:\n{transcript}\n\nОбновленный профиль:"
    )
