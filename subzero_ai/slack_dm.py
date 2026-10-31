"""Messaging the bot in Slack: a direct message goes through the same mentor approval as a website message,
and the AI's reply is posted back in the Slack thread.

One Slack thread is one chat. The first message of a thread starts a chat; replies in the thread continue it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from .approvals import ApprovalService
from .config import Settings
from .live import Tasks
from .render import EXPLAINER_TAG
from .roles import Directory, is_excluded_account
from .slack_handlers import viewer_for_slack_user
from .store import EDITED, ERROR, REJECTED, Store

log = logging.getLogger("subzero.slack")

CHUNK_LIMIT = 3500  # characters per Slack post; Slack allows more, but long posts get cut off in notifications
UPDATE_SECONDS = 3.0  # least time between edits of the reply while it streams
MAX_SOURCES = 5

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.*?)(?:\s+#+)?\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?![\s*])([^*\n]+?)(?<!\s)\*(?![\w*])")
_QUOTE = re.compile(r"^ {0,3}>\s?(.*)$")
_INLINE_CODE = re.compile(r"(`[^`\n]+`)")
_SENTINEL = "\x00{}\x00"


# --------------------------------------------------------------------------- formatting
def escape(text: str) -> str:
    """The three characters Slack treats as markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _split_blocks(text: str) -> list[tuple[bool, str, str]]:
    """Markdown cut into (is_code, info string, body) pieces. Text pieces hold whole lines; a code piece holds the
    lines between the fences. A fence that is never closed runs to the end."""
    blocks: list[tuple[bool, str, str]] = []
    text_lines: list[str] = []
    code_lines: list[str] = []
    fence = ""
    info = ""
    for line in text.replace("\r\n", "\n").split("\n"):
        if not fence:
            match = _FENCE.match(line)
            if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                if text_lines:
                    blocks.append((False, "", "\n".join(text_lines)))
                    text_lines = []
                fence, info = match.group(1), match.group(2).strip()
            else:
                text_lines.append(line)
        else:
            stripped = line.strip()
            if stripped.startswith(fence[0] * len(fence)) and set(stripped) == {fence[0]}:
                blocks.append((True, info, "\n".join(code_lines)))
                code_lines, fence, info = [], "", ""
            else:
                code_lines.append(line)
    if fence:
        blocks.append((True, info, "\n".join(code_lines)))
    if text_lines:
        blocks.append((False, "", "\n".join(text_lines)))
    return blocks


def replace_explainers(text: str, link: str) -> str:
    """Interactive explainers only run on the website: swap each one for a line pointing there."""
    out: list[str] = []
    for is_code, info, body in _split_blocks(text):
        if is_code and info.split(" ")[0].lower() == EXPLAINER_TAG:
            out.append(f"[This reply has an interactive explainer. Open it on the website]({link})")
        elif is_code:
            out.append(f"```{info}\n{body}\n```")
        else:
            out.append(body)
    return "\n".join(out)


def _inline(line: str) -> str:
    """Slack formatting for one line of Markdown that isn't inside a code block."""
    quote = _QUOTE.match(line)
    if quote:
        return "> " + _inline(quote.group(1))
    heading = _HEADING.match(line)
    if heading:
        line = "**" + heading.group(1).replace("**", "") + "**"
    line = _BULLET.sub(lambda m: m.group(1) + "• ", line)
    pieces = _INLINE_CODE.split(line)
    for index, piece in enumerate(pieces):
        if index % 2:  # inline code: only escape it
            pieces[index] = escape(piece)
            continue
        links: list[str] = []

        def keep_link(match: re.Match) -> str:
            links.append(f"<{escape(match.group(2))}|{_link_text(match.group(1))}>")
            return _SENTINEL.format(len(links) - 1)

        piece = escape(_LINK.sub(keep_link, piece))
        # bold first (set aside so its stars aren't taken for italics), then italics, then bold in Slack's form
        piece = _BOLD.sub(lambda m: "\x01" + (m.group(1) or m.group(2)) + "\x02", piece)
        piece = _ITALIC.sub(r"_\1_", piece).replace("\x01", "*").replace("\x02", "*")
        for number, value in enumerate(links):
            piece = piece.replace(_SENTINEL.format(number), value)
        pieces[index] = piece
    return "".join(pieces)


def _link_text(text: str) -> str:
    """Link text for Slack's <url|text>: a bar would end the text early."""
    return escape(text).replace("|", "¦")


def _cut_point(text: str, limit: int) -> int:
    """Where to cut text so a piece is at most `limit` long without splitting a <...> or an &...; entity."""
    if len(text) <= limit:
        return len(text)
    cut = limit
    start = text.rfind("<", 0, cut)
    if start != -1 and text.find(">", start, cut) == -1:
        cut = start
    amp = text.rfind("&", max(0, cut - 8), cut)
    if amp != -1 and text.find(";", amp, cut) == -1:
        cut = amp
    return cut if cut > 0 else limit


def _hard_split(text: str, limit: int) -> list[str]:
    pieces = []
    while text:
        cut = _cut_point(text, limit)
        pieces.append(text[:cut])
        text = text[cut:]
    return pieces or [""]


def to_slack(markdown: str) -> list[str]:
    """Markdown from the AI as Slack posts of at most CHUNK_LIMIT characters, cut between lines.

    Bold, headings, bullets and links become Slack's versions; & < > are escaped (Slack needs that everywhere).
    Code blocks are kept as they are, without their language word, and are only split when one is too long
    for a single post, in which case each part gets its own fences."""
    units: list[str] = []
    for is_code, _info, body in _split_blocks(markdown or ""):
        if is_code:
            units.append("```\n" + escape(body) + "\n```")
        else:
            units.extend(_inline(line) for line in body.split("\n"))
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip("\n"))
        current = ""

    for unit in units:
        if unit.startswith("```\n") and unit.endswith("\n```") and len(unit) > CHUNK_LIMIT:
            flush()
            lines = unit[4:-4].split("\n")
            part = ""
            for line in lines:
                for piece in _hard_split(line, CHUNK_LIMIT - 10):
                    if part and len(part) + len(piece) + 1 > CHUNK_LIMIT - 8:
                        chunks.append("```\n" + part + "\n```")
                        part = ""
                    part = part + "\n" + piece if part else piece
            if part:
                chunks.append("```\n" + part + "\n```")
            continue
        if len(unit) > CHUNK_LIMIT:
            flush()
            chunks.extend(_hard_split(unit, CHUNK_LIMIT))
            continue
        if current and len(current) + 1 + len(unit) > CHUNK_LIMIT:
            flush()
        current = current + "\n" + unit if current else unit
    flush()
    return chunks


# --------------------------------------------------------------------------- the service
class _Reply:
    """A reply being posted to Slack: the placeholder message and the job that keeps it up to date."""

    def __init__(self, channel: str, thread_ts: str, conversation_id: int):
        self.channel = channel
        self.thread_ts = thread_ts
        self.conversation_id = conversation_id
        self.ts: str | None = None
        self.posted = asyncio.Event()
        self.ticker: asyncio.Task | None = None
        self.last_text = ""
        self.done = False  # the final text has been posted


class SlackDMs:
    def __init__(self, *, settings: Settings, store: Store, approvals: ApprovalService, directory: Directory,
                 slack, tasks: Tasks, update_seconds: float = UPDATE_SECONDS):
        self.settings = settings
        self.store = store
        self.approvals = approvals
        self.runner = approvals.runner
        self.directory = directory
        self.slack = slack
        self.tasks = tasks
        self.update_seconds = update_seconds
        self._bot_user_id: str | None = None
        self._replies: dict[int, _Reply] = {}

    # ----------------------------------------------------------- incoming messages
    async def bot_user_id(self) -> str | None:
        if self._bot_user_id is None:
            try:
                self._bot_user_id = (await self.slack.auth_test()).get("user_id")
            except Exception:
                log.warning("Couldn't look up the bot's own Slack user id", exc_info=True)
        return self._bot_user_id

    @property
    def _site(self) -> str:
        return self.settings.url("/")

    async def _say(self, channel: str, thread_ts: str, text: str) -> bool:
        """Post in a thread. False if Slack refused."""
        try:
            await self.slack.post_message(channel, text, thread_ts=thread_ts)
            return True
        except Exception:
            log.warning("Couldn't post in Slack channel %s", channel, exc_info=True)
            return False

    async def handle_event(self, event: dict[str, Any]) -> None:
        """One message.im event. Never raises."""
        try:
            await self._handle(event)
        except Exception:
            log.exception("Couldn't handle a Slack direct message")

    async def _handle(self, event: dict[str, Any]) -> None:
        user, channel, ts = event.get("user"), event.get("channel"), event.get("ts")
        subtype = event.get("subtype")
        has_files = bool(event.get("files")) or subtype == "file_share"
        if not user or not channel or not ts or event.get("bot_id") or (subtype and subtype != "file_share"):
            return
        if user == await self.bot_user_id():
            return
        text = (event.get("text") or "").strip()
        if not text and not has_files:
            return
        if not self.store.claim_dm_event(channel, ts):
            return  # Slack delivered this message before
        root = event.get("thread_ts") or ts
        top_level = root == ts

        if not self.settings.slack_dms_enabled:
            await self._say(channel, root, f"Messaging the bot is turned off. Use the website: {self._site}")
            return
        if has_files:
            await self._say(channel, root, f"I can't take files in Slack yet. Attach them on the website instead: "
                                           f"{self._site}")
            return

        try:
            viewer = await self._viewer(user)
            if viewer is None:
                await self._say(channel, root, "Sorry, only members of the team's Slack workspace can message me. "
                                               "Ask a mentor if you think this is a mistake.")
                return
            conversation_id = None
            if not top_level:
                conversation_id = self.store.conversation_in_thread(channel, root)
                if conversation_id is None:
                    await self._say(channel, root, "I can only continue chats I started. Send a new message (not a "
                                                   "reply in a thread) to start a new chat.")
                    return
        except Exception:
            # Nothing was submitted: forget the message so Slack's retry is handled
            log.exception("Couldn't check a Slack direct message")
            self.store.release_dm_event(channel, ts)
            await self._say(channel, root, "Something went wrong on my side. Try again in a minute.")
            return

        def saved(new_conversation_id: int, message_id: int) -> None:
            if top_level:
                self.store.link_slack_thread(new_conversation_id, channel, root)
            self.store.link_dm_message(channel, ts, message_id)

        outcome = await self.approvals.submit(viewer, text, conversation_id, on_saved=saved)
        if not outcome.ok:
            await self._say(channel, root, outcome.message)
            return
        msg = self.store.message(outcome.message_id)
        if msg["status"] == "pending":
            note = "A mentor will check this before the AI answers. I'll reply here."
            if top_level:
                note += (f"\nThis chat is also on the website, where the team can see approved chats: "
                         f"{self.approvals.link(outcome.conversation_id)}")
            await self._say(channel, root, note)

    async def _viewer(self, slack_user_id: str):
        """The app account for this Slack member, or None if they can't use the bot."""
        info = await self.slack.user_info(slack_user_id)  # a failure is temporary: the caller asks to try again
        is_admin = slack_user_id in self.settings.admin_slack_ids
        if is_excluded_account(info, self.settings) and not is_admin:
            return None
        team = self.settings.slack_team_id
        if team and info.get("team_id") and info["team_id"] != team and not is_admin:
            return None
        row = self.store.user_by_slack_id(slack_user_id)
        if row is not None and not row["active"]:
            return None
        role = await self.directory.role_for(slack_user_id, force=False)
        return await viewer_for_slack_user(self.store, self.slack, slack_user_id, role, info)

    # ----------------------------------------------------------- posting replies
    def _thread_of(self, user_message_id: int):
        """(channel, thread_ts) of the Slack thread a message came in on, or None for a website message."""
        if self.store.dm_message(user_message_id) is None:
            return None
        msg = self.store.message(user_message_id)
        row = self.store.slack_thread(msg["conversation_id"]) if msg else None
        return (row["channel"], row["thread_ts"]) if row else None

    async def on_reply(self, conversation_id: int, user_message_id: int, reply_id: int) -> None:
        """A reply started: post a placeholder and keep it updated while the text streams."""
        target = self._thread_of(user_message_id)
        if target is None:
            return
        state = self._replies[reply_id] = _Reply(*target, conversation_id)
        try:
            posted = await self.slack.post_message(state.channel, "Thinking…", thread_ts=state.thread_ts)
            state.ts = posted["ts"]
            if state.done:
                # The answer was posted without waiting for this placeholder; it's left behind, so point to the answer
                await self.slack.update_message(state.channel, state.ts, "(The answer is below.)")
            else:
                state.ticker = self.tasks.spawn(self._keep_updated(reply_id, state), name=f"slack-reply-{reply_id}")
        except Exception:
            log.warning("Couldn't post the placeholder for reply %s", reply_id, exc_info=True)
        finally:
            state.posted.set()

    async def _keep_updated(self, reply_id: int, state: _Reply) -> None:
        while not state.done and self._replies.get(reply_id) is state:
            await asyncio.sleep(self.update_seconds)
            if state.done:
                break
            text, _ = self.runner.live_text(reply_id)
            if not text.strip() or text == state.last_text:
                continue
            state.last_text = text
            try:
                first = to_slack(replace_explainers(text, self.approvals.link(state.conversation_id)))
                if first:
                    await self.slack.update_message(state.channel, state.ts, first[0])
            except Exception:
                log.warning("Couldn't update the Slack reply %s", reply_id, exc_info=True)

    async def on_reply_done(self, conversation_id: int, user_message_id: int, reply_id: int) -> None:
        """The reply is saved: replace the placeholder with it."""
        if self.store.dm_message(user_message_id) is None:
            return
        state = self._replies.get(reply_id)
        if state is None:
            thread = self._thread_of(user_message_id)
            if thread is None:
                return
            state = _Reply(*thread, conversation_id)
            state.posted.set()
        try:
            await asyncio.wait_for(state.posted.wait(), 20)
        except asyncio.TimeoutError:
            pass
        state.done = True
        if state.ticker is not None:
            state.ticker.cancel()
            await asyncio.gather(state.ticker, return_exceptions=True)
        self._replies.pop(reply_id, None)
        reply = self.store.message(reply_id)
        link = self.approvals.link(conversation_id)
        if reply["status"] == ERROR:
            chunks = [f"Sorry, the AI couldn't finish that reply. You can try again on the website: {link}"]
        else:
            chunks = self.reply_chunks(reply["content"], reply["sources"], link)
        for index, chunk in enumerate(chunks):
            try:
                if index == 0 and state.ts:
                    try:
                        await self.slack.update_message(state.channel, state.ts, chunk)
                        continue
                    except Exception:
                        log.warning("Couldn't update the Slack placeholder for reply %s; posting instead", reply_id,
                                    exc_info=True)
                await self.slack.post_message(state.channel, chunk, thread_ts=state.thread_ts)
            except Exception:
                log.warning("Couldn't post part %s of reply %s in Slack", index + 1, reply_id, exc_info=True)

    @staticmethod
    def reply_chunks(content: str, sources_json: str | None, link: str) -> list[str]:
        """The reply as Slack posts: explainers swapped for a link, and the sources listed at the end."""
        chunks = to_slack(replace_explainers(content, link))
        try:
            sources = [s for s in json.loads(sources_json or "[]") if s.get("url")][:MAX_SOURCES]
        except ValueError:
            sources = []
        if sources:
            lines = [f"• <{escape(s['url'])}|{_link_text(s.get('title') or s['url'])}>" for s in sources]
            block = "Sources:\n" + "\n".join(lines)
            if chunks and len(chunks[-1]) + 2 + len(block) <= CHUNK_LIMIT:
                chunks[-1] += "\n\n" + block
            else:
                chunks.append(block)
        return chunks

    # ----------------------------------------------------------- mentor decisions
    async def on_decided(self, message_id: int) -> bool:
        """A mentor decided on a message. If it came in on Slack, tell the student in the thread (instead of the
        usual DM) and return True. Only the student sees a DM thread, so a rejection reason stays with them."""
        if self.store.dm_message(message_id) is None:
            return False
        thread = self._thread_of(message_id)
        if thread is None:
            return False
        msg = self.store.message(message_id)
        if msg["status"] == REJECTED:
            reason = escape(msg["review_note"] or "")
            return await self._say(thread[0], thread[1], f"A mentor didn't approve this: {reason}")
        elif msg["status"] == EDITED:
            return await self._say(thread[0], thread[1], "A mentor edited your message before the AI saw it.")
        return True
