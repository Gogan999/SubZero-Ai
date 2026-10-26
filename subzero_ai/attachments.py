"""File attachments: checking uploads, storing them, and turning them into content Claude can read."""

from __future__ import annotations

import base64
import io
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .db import Database, utcnow

TEXT_EXTENSIONS = {".java", ".py", ".cpp", ".h", ".json", ".gradle", ".yaml", ".yml", ".txt", ".md", ".csv"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
VIDEO_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
ACCEPT = ",".join(sorted(TEXT_EXTENSIONS | IMAGE_EXTENSIONS | set(VIDEO_TYPES)))

MAX_FILES = 5
MAX_FILE_BYTES = 50 * 1024 * 1024
# Cloudflare's free plan refuses uploads over 100 MB in one request, so one message stays under that
MAX_MESSAGE_BYTES = 90 * 1024 * 1024
# Claude can't watch video. The browser takes this many still frames from each video and sends them along.
MAX_FRAMES_PER_VIDEO = 8
FRAME_MAX_BYTES = 5 * 1024 * 1024
# Claude reads images best at up to about 1568 pixels on the long edge; bigger ones cost more and gain nothing.
IMAGE_MAX_EDGE = 1568
# Refuse images that would take a lot of memory to open (a phone photo is about 12 million pixels).
# A tiny PNG can claim huge dimensions, so this is checked from the header before any pixels are decoded.
MAX_IMAGE_PIXELS = 50_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS  # Pillow's own backstop (it only refuses above twice this)


class AttachmentError(Exception):
    """A problem with an upload, worded for the student."""


@dataclass
class Prepared:
    name: str
    kind: str  # "text", "image", "video", or "frame" (a still from a video, for Claude; not shown on the page)
    media_type: str
    data: bytes  # UTF-8 text, the re-encoded image, or the video as uploaded

    @property
    def size(self) -> int:
        return len(self.data)


def size_label(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB" if size >= 10 * 1024 else f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def clean_name(filename: str | None) -> str:
    """Just the file name: no folders, no control characters, and not too long."""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C").strip().strip(".")
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:110] + dot + ext) if dot and len(ext) <= 8 else name[:120]
    return name or "file"


def _extension(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def prepare(filename: str | None, data: bytes, *, max_text_bytes: int) -> Prepared:
    """Check one upload and convert it to what gets stored. Raises AttachmentError with a reason to show."""
    name = clean_name(filename)
    ext = _extension(name)
    if len(data) > MAX_FILE_BYTES:
        raise AttachmentError(f"{name} is over 50 MB. Attach a smaller file, or trim the video to the part "
                              "you're asking about.")
    if not data:
        raise AttachmentError(f"{name} is empty.")
    if ext in TEXT_EXTENSIONS:
        return _prepare_text(name, data, max_text_bytes)
    if ext in IMAGE_EXTENSIONS:
        return _prepare_image(name, data)
    if ext in VIDEO_TYPES:
        return _prepare_video(name, ext, data)
    allowed = ", ".join(sorted(TEXT_EXTENSIONS | {".png", ".jpg"}))
    raise AttachmentError(f"{name} can't be attached. Use a code or text file ({allowed}), a PNG or JPG "
                          "screenshot, or an MP4, MOV or WebM video.")


def _prepare_video(name: str, ext: str, data: bytes) -> Prepared:
    """Videos are stored as sent (re-encoding needs tools the mini PC doesn't have), after checking that the
    file really is the kind of video its name says, so nothing else can be served as a video."""
    is_mp4 = data[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip")
    is_webm = data[:4] == b"\x1a\x45\xdf\xa3"
    if not (is_webm if ext == ".webm" else is_mp4):
        raise AttachmentError(f"{name} doesn't look like a video. Use an MP4, MOV or WebM file.")
    return Prepared(name, "video", VIDEO_TYPES[ext], data)


def prepare_frame(video_name: str, seconds: int, data: bytes) -> Prepared:
    """One still from a video, taken by the browser. It's checked and re-encoded like any image."""
    frame = _prepare_image(f"{video_name} frame", data)
    total = max(0, seconds)
    frame.name = f"{video_name} at {total // 60}:{total % 60:02d}"
    frame.kind = "frame"
    return frame


def _prepare_text(name: str, data: bytes, max_text_bytes: int) -> Prepared:
    if b"\x00" in data:
        raise AttachmentError(f"{name} doesn't look like a text file.")
    if len(data) > max_text_bytes:
        raise AttachmentError(f"{name} is {size_label(len(data))}. Text files can be up to {size_label(max_text_bytes)}, "
                              "so attach just the files or parts you're asking about.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")  # older Windows editors
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return Prepared(name, "text", "text/plain", text.encode("utf-8"))


def _prepare_image(name: str, data: bytes) -> Prepared:
    """Re-encode the image: this drops hidden details such as a phone photo's GPS location,
    applies the camera's rotation, and shrinks big photos to the size Claude actually uses."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format not in ("PNG", "JPEG"):
                raise AttachmentError(f"{name} isn't a PNG or JPG image.")
            fmt = img.format
            if img.width * img.height > MAX_IMAGE_PIXELS:
                raise AttachmentError(f"{name} is too large an image. Send a screenshot or a smaller photo.")
            img.load()
            img = ImageOps.exif_transpose(img)
            img.thumbnail((IMAGE_MAX_EDGE, IMAGE_MAX_EDGE))
            out = io.BytesIO()
            if fmt == "PNG":
                if img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                    img = img.convert("RGBA")
                img.save(out, "PNG", optimize=True)
                media_type = "image/png"
            else:
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(out, "JPEG", quality=85, optimize=True)
                media_type = "image/jpeg"
    except AttachmentError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        raise AttachmentError(f"{name} couldn't be opened as an image.") from None
    return Prepared(name, "image", media_type, out.getvalue())


def check_count(count: int) -> None:
    if count > MAX_FILES:
        raise AttachmentError(f"You can attach up to {MAX_FILES} files to one message.")


class Attachments:
    """Stored attachments. Files live in DATA_DIR/files; the database keeps their names and sizes."""

    def __init__(self, db: Database, files_dir: Path):
        self.db = db
        self.files_dir = Path(files_dir)

    def save(self, conn: sqlite3.Connection, message_id: int, item: Prepared) -> int:
        if item.kind == "video":
            ext = _extension(item.name)
        else:
            ext = ".png" if item.media_type == "image/png" else ".jpg" if item.kind in ("image", "frame") else ".txt"
        relative = f"{message_id}-{secrets.token_hex(8)}{ext}"
        path = self.files_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item.data)
        cur = conn.execute(
            "INSERT INTO attachments (message_id, name, kind, media_type, size, path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, item.name, item.kind, item.media_type, item.size, relative, utcnow()),
        )
        return cur.lastrowid

    def get(self, attachment_id: int) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM attachments WHERE id = ?", (attachment_id,))

    def for_message(self, message_id: int) -> list[sqlite3.Row]:
        return self.db.all("SELECT * FROM attachments WHERE message_id = ? ORDER BY id", (message_id,))

    def for_conversation(self, conversation_id: int) -> dict[int, list[sqlite3.Row]]:
        rows = self.db.all(
            "SELECT a.* FROM attachments a JOIN messages m ON m.id = a.message_id "
            "WHERE m.conversation_id = ? ORDER BY a.id", (conversation_id,))
        out: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            out.setdefault(row["message_id"], []).append(row)
        return out

    def path(self, row: sqlite3.Row) -> Path:
        path = (self.files_dir / row["path"]).resolve()
        if self.files_dir.resolve() not in path.parents or not path.is_file():
            raise FileNotFoundError(row["path"])
        return path

    def read(self, row: sqlite3.Row) -> bytes:
        return self.path(row).read_bytes()

    def claude_blocks(self, rows: list[sqlite3.Row], *, images: bool = True) -> list[dict]:
        """Content blocks for the Messages API: text files in <file> tags, images as base64.
        With images=False (older messages, see generation.py), pictures become a short note instead."""
        blocks: list[dict] = []
        for row in rows:
            name = row["name"].replace('"', "'")
            if row["kind"] == "video":
                frames = sum(1 for r in rows if r["kind"] == "frame" and r["name"].startswith(row["name"] + " at "))
                if frames:
                    note = (f"The student attached this video. You can't watch video, so the {frames} still frames "
                            "after this, taken at even steps through it, are what you get. Say what you can and "
                            "can't tell from stills.")
                else:
                    note = ("The student attached this video, but no still frames could be taken from it, so you "
                            "can't see it. Ask them to describe what happens or attach screenshots.")
                blocks.append({"type": "text", "text": f'<video name="{name}">{note}</video>'})
                continue
            if row["kind"] == "frame":
                blocks.append({"type": "text", "text": f"Still frame: {name}"})
            if row["kind"] in ("image", "frame") and not images:
                blocks.append({"type": "text", "text": f'<file name="{name}">(Image shown earlier in the chat; '
                                                      "it isn't sent again.)</file>"})
                continue
            try:
                data = self.read(row)
            except OSError:
                blocks.append({"type": "text", "text": f'<file name="{name}">(This file is missing.)</file>'})
                continue
            if row["kind"] in ("image", "frame"):
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": row["media_type"], "data": base64.b64encode(data).decode("ascii")}})
            else:
                text = data.decode("utf-8", errors="replace")
                blocks.append({"type": "text", "text": f'<file name="{name}">\n{text}\n</file>'})
        return blocks
