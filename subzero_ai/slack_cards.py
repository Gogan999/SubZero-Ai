"""Block Kit layouts for approval cards, modals, alerts, and DMs."""

from __future__ import annotations

from datetime import datetime

from .attachments import size_label

CARD_TEXT_LIMIT = 2800
MODAL_TEXT_LIMIT = 2900


def _mrkdwn_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.splitlines()) or ">"


def _slack_time(iso: str | None) -> str:
    if not iso:
        return ""
    stamp = int(datetime.fromisoformat(iso).timestamp())
    return f"<!date^{stamp}^{{date_short_pretty}} at {{time}}|{iso}>"


def card_header(student_name: str, subteam: str | None, title: str | None, link: str) -> str:
    parts = [f"*{_mrkdwn_escape(student_name)}*"]
    if subteam:
        parts.append(_mrkdwn_escape(subteam))
    parts.append(f"<{link}|{_mrkdwn_escape(title or 'New chat')}>")
    return " · ".join(parts)


def card_body(text: str, link: str) -> str:
    body = text
    if len(body) > CARD_TEXT_LIMIT:
        body = body[:CARD_TEXT_LIMIT].rstrip() + "…"
    quoted = _quote(_mrkdwn_escape(body))
    if len(text) > CARD_TEXT_LIMIT:
        quoted += f"\n<{link}|Open in SubZero AI to read the whole message>"
    return quoted


def files_line(files: list[tuple[str, int]], link: str) -> str:
    """Slack can't load files from behind the app's sign-in, so the card lists them and links to the chat."""
    names = ", ".join(f"{_mrkdwn_escape(name)} ({size_label(size)})" for name, size in files)
    word = "file" if len(files) == 1 else "files"
    return f":paperclip: {len(files)} {word}: {names}. <{link}|View in SubZero AI>"


def approval_card(*, message_id: int, student_name: str, subteam: str | None, title: str | None,
                  text: str, link: str, previous_reply: str | None = None,
                  files: list[tuple[str, int]] | None = None, explainer: bool = False,
                  project: str | None = None, tools: list[str] | None = None) -> list[dict]:
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": card_header(student_name, subteam, title, link)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": card_body(text, link)}},
    ]
    extras = []
    if project:
        extras.append({"type": "mrkdwn", "text": "In the project *" + _mrkdwn_escape(project) + "*, so Claude also "
                                                 "reads its instructions and files"})
    if files:
        extras.append({"type": "mrkdwn", "text": files_line(files, link)})
    if tools:
        extras.append({"type": "mrkdwn", "text": "Asked for: " + _mrkdwn_escape(", ".join(tools))
                       + (" (web searches cost extra)" if any("search" in t for t in tools) else "")})
    if explainer:
        extras.append({"type": "mrkdwn", "text": ":bulb: Asked for an interactive explainer (costs a little more)"})
    if extras:
        blocks.append({"type": "context", "elements": extras})
    if previous_reply:
        snippet = previous_reply.strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:200].rstrip() + "…"
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": "Replying to Claude: " + _mrkdwn_escape(snippet)}]})
    value = str(message_id)
    blocks.append({
        "type": "actions",
        "block_id": f"review_{message_id}",
        "elements": [
            {"type": "button", "action_id": "approve", "style": "primary", "value": value,
             "text": {"type": "plain_text", "text": "Approve"}},
            {"type": "button", "action_id": "edit", "value": value,
             "text": {"type": "plain_text", "text": "Edit"}},
            {"type": "button", "action_id": "reject", "style": "danger", "value": value,
             "text": {"type": "plain_text", "text": "Reject"}},
            {"type": "button", "action_id": "handle_private", "value": value,
             "text": {"type": "plain_text", "text": "Handle privately"},
             "confirm": {
                 "title": {"type": "plain_text", "text": "Handle privately?"},
                 "text": {"type": "plain_text", "text": "Nothing goes to Claude, and the chat is hidden from the team. "
                                                       "The student sees that a mentor will follow up with them directly."},
                 "confirm": {"type": "plain_text", "text": "Handle privately"},
                 "deny": {"type": "plain_text", "text": "Cancel"},
             }},
        ],
    })
    return blocks


OUTCOME_WORDS = {
    "approved": "Approved",
    "edited": "Edited and approved",
    "rejected": "Rejected",
    "held": "Held privately",
    "withdrawn": "Withdrawn by the student",
}


def handled_card(*, student_name: str, subteam: str | None, title: str | None, text: str, link: str,
                 outcome: str, mentor_slack_id: str | None, acted_at: str | None,
                 reason: str | None = None) -> list[dict]:
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": card_header(student_name, subteam, title, link)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": card_body(text, link)}},
    ]
    words = OUTCOME_WORDS.get(outcome, outcome.capitalize())
    line = words
    if mentor_slack_id:
        line += f" by <@{mentor_slack_id}>"
    if acted_at:
        line += " " + _slack_time(acted_at)
    if outcome == "rejected" and reason:
        line += f". Reason: {_mrkdwn_escape(reason)}"
    if outcome == "held":
        line += ". Follow up with the student in person."
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": line}]})
    return blocks


def card_fallback_text(student_name: str, outcome: str | None = None) -> str:
    if outcome:
        return f"{student_name}'s message: {OUTCOME_WORDS.get(outcome, outcome)}"
    return f"{student_name} sent a message for review"


def edit_modal(message_id: int, text: str) -> dict:
    return {
        "type": "modal",
        "callback_id": "edit_submit",
        "private_metadata": str(message_id),
        "title": {"type": "plain_text", "text": "Edit and approve"},
        "submit": {"type": "plain_text", "text": "Approve edited version"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "input", "block_id": "edited", "label": {"type": "plain_text", "text": "Message Claude will see"},
             "element": {"type": "plain_text_input", "action_id": "text", "multiline": True,
                         "initial_value": text, "max_length": 3000}},
            {"type": "context", "elements": [{"type": "mrkdwn",
             "text": "The student sees your version marked as edited. The original is kept in the audit log."}]},
        ],
    }


def too_long_to_edit_modal(link: str) -> dict:
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Edit in SubZero AI"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                    "text": f"This message is too long to edit in Slack. <{link}|Open it in SubZero AI> to edit it there."}}],
    }


def reject_modal(message_id: int) -> dict:
    return {
        "type": "modal",
        "callback_id": "reject_submit",
        "private_metadata": str(message_id),
        "title": {"type": "plain_text", "text": "Reject message"},
        "submit": {"type": "plain_text", "text": "Reject"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "input", "block_id": "reason", "label": {"type": "plain_text", "text": "Reason the student will see"},
             "element": {"type": "plain_text_input", "action_id": "text", "multiline": True, "max_length": 500,
                         "placeholder": {"type": "plain_text", "text": "For example: ask about one subsystem at a time"}}},
        ],
    }


def alert_card(*, student_name: str, title: str | None, link: str) -> list[dict]:
    text = (f":rotating_light: *Mentor alert* in {_mrkdwn_escape(student_name)}'s chat "
            f"<{link}|{_mrkdwn_escape(title or 'New chat')}>. Claude flagged the student may need support. "
            "The chat is now hidden from the team. Please follow up in person.")
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def dm_text(outcome: str, *, title: str | None, link: str, mentor_name: str | None, reason: str | None = None) -> str:
    chat = f"“{_mrkdwn_escape(title)}”" if title else "your new chat"
    who = _mrkdwn_escape(mentor_name or "A mentor")
    if outcome == "approved":
        return f"{who} approved your message in {chat}. Claude is answering now: {link}"
    if outcome == "edited":
        return f"{who} edited and approved your message in {chat}. Claude is answering now: {link}"
    if outcome == "rejected":
        return (f"{who} didn't approve your message in {chat}. Reason: {_mrkdwn_escape(reason or 'none given')}. "
                f"You can send a revised message: {link}")
    raise ValueError(f"No DM for outcome {outcome}")
