"""Prompts for the LLM classification step (§5)."""
from __future__ import annotations

import json
from typing import Any, Dict, List

SYSTEM_PROMPT = """\
You extract commitments, follow-ups and reminders from a person's private \
conversations. The conversations are casual — people almost never phrase \
these as tasks. Your job is to notice the obligation hiding inside ordinary \
chat, and to ignore everything that is just talk.

TAXONOMY — every extracted item is exactly one of:
  purchase  something the user should buy or acquire.
            e.g. "I want that bag" -> entity is the product.
  event     a dated thing to be aware of or attend.
            e.g. "Grandma's 80th is in 3 weeks" -> entity is the date.
  promise   something the user (or the sender, on the user's behalf) committed to.
            e.g. "I'll get the hoop for his birthday" -> entity is the commitment.
  followup  a nudge about something already outstanding.
            e.g. "Did you look at that paper I sent?"
  reading   an article, book, link or recommendation to consume.
            e.g. "Found this article, you'd love it" -> entity is the link/title.
  question  anything that plainly requires a direct reply from the user.

RULES
- Extract only messages that imply the USER should do, decide, buy, attend, \
read or answer something. The sender narrating their own life is not an item.
- One message can yield zero, one, or more items. Most messages yield zero.
- Never invent an item to be helpful. Silence is the correct output for chatter, \
jokes, logistics already settled, and pure acknowledgements.
- Marketing email, newsletters and automated notifications are not items unless \
they carry a real obligation with a deadline for this user.
- A `[attachment: <filename>]` section is the text of a file the message \
carried — a bill, a form, a statement. It counts as part of the message: an \
obligation stated only inside the attachment is still an obligation, and its \
`entities.item` should name what the document says, not the filename.
- `suggested_action` is a short imperative the user could act on directly, \
including the deadline when one exists: "Buy the Lemaire croissant bag by Friday".
- `suggested_reply` is a natural one- or two-sentence reply in the user's voice: \
low-key, lowercase-ish, not corporate. Null when a reply is not what is wanted.
- `entities.date`: copy the date language verbatim from the message into \
`date_phrase` (e.g. "in 3 weeks", "by Friday"). Do not compute a calendar date; \
the caller normalises it against the message timestamp.
- `entities.item`: the concrete noun — product, commitment, or document.
- `entities.link`: a URL if the message contains one, else null.
- `confidence` is 0..1 — how sure you are this is a real obligation.

WORLD FACTS — a second, separate output. Alongside the items, report what \
each message STATES about the user's world: who people are, where they are \
enrolled or employed, what organisations and places they deal with, standing \
arrangements (accounts, memberships, appointments that recur). These are \
facts, not tasks — "Lia attends Lakeview preschool" is a world fact \
even though nobody has to do anything about it.
- Only what a message states or directly implies. Never infer mood, \
character, or anything about the user's inner life.
- `kind` is person|place|org|arrangement. A child mentioned by name is a \
person even if they never send messages.
- `predicate` is a short snake_case relation: attends, works_at, located_at, \
account_with, role_of, relation_to_user, teacher_of, enrolled_in, member_of.
- `relation_to_user` values come from a CLOSED list: wife, husband, partner, \
son, daughter, mother, father, brother, sister, family, friend, colleague, \
client, service, teacher, neighbor, acquaintance. If none fits, omit the \
claim — "visited user's home" is an event, not a relationship.
- Most messages state nothing about the world; an empty list is the normal \
output.

Return JSON only. No prose, no markdown fences."""

OUTPUT_CONTRACT = """\
Return a JSON object of this exact shape:

{
  "items": [
    {
      "message_id": "<the id of the source message>",
      "type": "purchase|event|promise|followup|reading|question",
      "entities": {"item": string|null, "date_phrase": string|null, "link": string|null},
      "suggested_action": string,
      "suggested_reply": string|null,
      "confidence": number
    }
  ],
  "entities": [
    {
      "message_id": "<the id of the message that stated it>",
      "name": "<canonical name>",
      "kind": "person|place|org|arrangement",
      "claims": [{"predicate": string, "value": string}],
      "confidence": number
    }
  ]
}

If a message contains neither, return {"items": [], "entities": []}."""


def build_user_prompt(batch: List[Dict[str, Any]], draft_replies: bool = True) -> str:
    """`batch` entries: id, person, timestamp, source, text, thread_context."""
    lines = [
        "Here is a batch of messages from the user's conversations.",
        "Extract items per the taxonomy.",
        "",
    ]
    if not draft_replies:
        lines.append('The user has disabled drafted replies: always set "suggested_reply" to null.')
        lines.append("")

    for entry in batch:
        lines.append(f"--- message {entry['id']}")
        lines.append(f"source: {entry['source']}")
        lines.append(f"from: {entry['person']}")
        lines.append(f"sent: {entry['timestamp']}")
        if entry.get("thread_context"):
            lines.append("recent thread context (oldest first, for reference only —")
            lines.append("do NOT extract items from these, they are already processed):")
            for ctx in entry["thread_context"]:
                lines.append(f"  [{ctx['who']}] {ctx['text']}")
        lines.append(f"text: {entry['text']}")
        lines.append("")

    lines.append(OUTPUT_CONTRACT)
    return "\n".join(lines)


def build_link_prompt(new_item: Dict[str, Any], candidates: List[Dict[str, Any]]) -> str:
    """§5: a followup should link back to the earlier item it refers to."""
    return (
        "A new follow-up item was extracted. Decide which earlier open item, if "
        "any, it refers to.\n\n"
        f"New follow-up: {json.dumps(new_item, indent=2)}\n\n"
        f"Open items from the same person:\n{json.dumps(candidates, indent=2)}\n\n"
        'Return JSON: {"links_to_item_id": "<id>"} or {"links_to_item_id": null}.'
    )


# --------------------------------------------------------- Phase D batch reply
BATCH_REPLY_SYSTEM = (
    "You write the user's outgoing text message. You are given several open "
    "loops the user owes ONE person, each with the user's own intended answer. "
    "Fold them into a SINGLE natural reply in the user's voice — warm, brief, "
    "the way a real person texts. Cover every loop; keep the user's own take on "
    "each verbatim in spirit; don't invent facts, commitments, or details that "
    "aren't in the answers. No greeting-card fluff, no 'I hope this finds you'. "
    "One short paragraph, at most two."
)


def build_batch_reply_prompt(person_name: str, items: List[Dict[str, Any]]) -> str:
    """Fold several owed items to one person into one outgoing reply."""
    lines = [f"Recipient: {person_name}", "", "Open loops to answer in one message:"]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. type: {it.get('type')}")
        if it.get("raw_text"):
            lines.append(f"   they said: {it['raw_text']}")
        if it.get("suggested_action"):
            lines.append(f"   what you owe: {it['suggested_action']}")
        if it.get("suggested_reply"):
            lines.append(f"   your intended answer: {it['suggested_reply']}")
        if it.get("entity_item"):
            lines.append(f"   about: {it['entity_item']}")
        lines.append("")
    lines.append('Return JSON only: {"reply": "<the single message>"}.')
    return "\n".join(lines)
