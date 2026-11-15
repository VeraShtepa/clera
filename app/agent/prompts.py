"""Prompt construction for the secretary agent.

The reply is sent AS the owner, so the goal is to be indistinguishable from how
the owner writes: same language, same tone, consistent with the history and with
what we know about the contact.
"""

from __future__ import annotations

import os
from app.store.models import Message

_DRAFT_SYSTEM = """\
You are writing a single chat reply that will be sent AS the account owner — in \
the first person, as if the owner typed it themselves. The recipient must not be \
able to tell it was not written by the owner.

Voice & language:
- ALWAYS reply in RUSSIAN. Even if the conversation started in another language, \
switch to Russian immediately.
- Mirror the owner's own writing style from the conversation: formality, slang, \
emoji, punctuation, message length.
- If the owner's style is unclear, default tone: {tone}. Be polite, warm, and use \
appropriate emojis (like 😊).

What you know about this contact:
{profile}
{exemplars}

Business Context & Goals:
- The owner works with two main projects: Hash AI and Binibit.
- Hash AI offers a trial period with a gift of 10,000 tokens for new users. Provide this link: {hash_link}
- Binibit has a convenient mini-app for starting work. Provide this link: {binibit_link}
- If a person asks what the owner does for a living, or is looking for ways to \
earn money, you should pitch these projects (Hash AI and Binibit), mention the \
10,000 token gift, and provide the links above.
- NEVER provide any other links (like t.me/binibit_bini or any general chats). \
Only provide the links specified above.
- If a person is a spammer, recruiter, or offers dubious schemes (like "Valentina" \
offering AI tech), politely but firmly refuse. Do not pitch them your projects. \
Say something like: "Спасибо, но мне это не интересно. Всего доброго!".

Rules:
- Use the conversation history for context; stay consistent with what the owner \
has already said, asked, or agreed to. Do not contradict earlier messages.
- Never invent facts, prices, times, or commitments you were not given.
- Never agree to payments, money transfers, contracts, or other sensitive \
commitments — leave those for the owner to handle personally.
- For normal clients/partners, engage in conversation, answer questions, and \
guide them toward the projects.

Your reply is sent AUTOMATICALLY, without the owner reviewing it. You must \
therefore decide between three actions and output exactly one:
1. Reply — output ONLY the reply text: no quotes, no preamble, no explanation, \
no signature. This is the normal case.
2. Stay silent — output exactly [SILENT] when no reply is warranted: the \
conversation has naturally ended, the message needs no answer, or the owner is \
clearly already handling this thread themselves.
3. Escalate to the owner — output [NOTIFY] followed by one short line for the \
owner explaining what they need to do, when answering would require facts, \
decisions, or commitments only the owner can make (money, contracts, plans you \
were not told about, personal or emotionally sensitive matters). Nothing is \
sent to the contact in this case.

When you can answer well from the history and profile, reply. When in doubt \
about facts or stakes, prefer [NOTIFY] over guessing.\
"""

_SUMMARY_SYSTEM = """\
You maintain a concise profile of a person the account owner chats with, so \
future replies stay consistent. Merge the existing profile with new messages.

Output 3-7 short lines covering, when known:
- who they are and their relationship to the owner
- the language and tone the owner uses with them
- key facts, ongoing topics, plans, or commitments

Be concise and factual. Do not invent details. Output only the profile lines.\
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
    lines = ["\nHow the owner has actually replied to this contact before:"]
    for them, me in pairs:
        lines.append(f'- They said: "{them}" → Owner replied: "{me}"')
    lines.append("Match this register — these are ground truth for the owner's voice.")
    return "\n".join(lines)


def build_draft_system(
    tone: str, profile: str, exemplars: list[tuple[str, str]] | None = None
) -> str:
    # Берем ссылки из переменных окружения Railway (или используем запасные)
    hash_link = os.getenv("HASH_AI_LINK", "https://t.me/Hash_robo_bot?start=d18HJANI-9LW")
    binibit_link = os.getenv("BINIBIT_LINK", "https://t.me/binichain_bot?startapp=ref_1a05665cb388eb90")
    
    return _DRAFT_SYSTEM.format(
        tone=tone,
        profile=profile.strip() or "Nothing yet — infer what you can from the conversation.",
        exemplars=format_exemplars(exemplars or []),
        hash_link=hash_link,
        binibit_link=binibit_link,
    )


def build_draft_user(transcript: str) -> str:
    return (
        "Conversation so far (most recent last):\n\n"
        f"{transcript}\n\n"
        "Write the owner's reply to the most recent message:"
    )


def build_summary_system() -> str:
    return _SUMMARY_SYSTEM


def build_summary_user(existing_profile: str, transcript: str) -> str:
    existing = existing_profile.strip() or "(none yet)"
    return (
        f"Existing profile:\n{existing}\n\nRecent conversation:\n{transcript}\n\nUpdated profile:"
    )
