"""Optional 2 GB uploads over the bot's own MTProto session.

api.telegram.org accepts at most 50 MB per uploaded file. When the owner sets
TELEGRAM_API_ID and TELEGRAM_API_HASH (created at https://my.telegram.org), a
file above that limit is uploaded by the same bot with Telethon. Telegram
returns the stored document, and the normal Bot API request then sends it by
file_id, so captions, buttons, replies and forum topics behave as before.
Nothing here receives updates or reads chats.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import logging
import mimetypes
import os
import re
import struct
import time
from pathlib import Path

LOG = logging.getLogger('veltrix.mtproto')
# TDLib file types used inside Bot API file identifiers.
FILE_TYPES = {'video': 4, 'document': 5, 'audio': 9, 'animation': 10}
FILE_REFERENCE_FLAG = 1 << 25
PERSISTENT_ID_VERSION = 4
# Bot API decodes identifiers from older TDLib versions; this layout carries
# the file reference that a freshly uploaded document needs.
PERSISTENT_ID_MINOR = 30
PART_SIZE_KB = 512
_client = None
_client_loop = None
_lock: asyncio.Lock | None = None
_disabled_until = 0.0


def api_credentials() -> tuple[int, str]:
    try:
        api_id = int(os.getenv('TELEGRAM_API_ID', '').strip() or 0)
    except ValueError:
        api_id = 0
    return api_id, os.getenv('TELEGRAM_API_HASH', '').strip()


def configured() -> bool:
    api_id, api_hash = api_credentials()
    if api_id <= 0 or not re.fullmatch(r'[0-9a-fA-F]{32}', api_hash):
        return False
    # Telethon is imported only for an actual large upload (it adds ~40 MB).
    return importlib.util.find_spec('telethon') is not None


def available() -> bool:
    return configured() and time.monotonic() >= _disabled_until


def mark_failed(exc: BaseException) -> None:
    """Pause large uploads after a failure; Telegram's flood wait is honoured."""
    global _disabled_until
    seconds = getattr(exc, 'seconds', None) or getattr(getattr(exc, '__cause__', None), 'seconds', None)
    try:
        pause = max(60.0, float(seconds)) if seconds else 600.0
    except (TypeError, ValueError):
        pause = 600.0
    _disabled_until = time.monotonic() + pause
    LOG.warning('Large uploads paused for %ss after %s', int(pause), type(exc.__cause__ or exc).__name__)


def _tl_bytes(data: bytes) -> bytes:
    if len(data) <= 253:
        out = bytes([len(data)]) + data
    else:
        out = b'\xfe' + len(data).to_bytes(3, 'little') + data
    return out + b'\x00' * (-len(out) % 4)


def _zero_encode(data: bytes) -> bytes:
    """TDLib's run-length encoding of zero bytes (runs of at most 250)."""
    out = bytearray()
    count = 0
    for byte in data:
        if byte == 0:
            count += 1
            if count == 250:
                out += bytes((0, count))
                count = 0
            continue
        if count:
            out += bytes((0, count))
            count = 0
        out.append(byte)
    if count:
        out += bytes((0, count))
    return bytes(out)


def bot_file_id(kind: str, dc_id: int, media_id: int, access_hash: int, file_reference: bytes) -> str:
    """Encode an uploaded document as a Bot API ``file_id``."""
    file_type = FILE_TYPES[kind] | (FILE_REFERENCE_FLAG if file_reference else 0)
    data = struct.pack('<ii', file_type, dc_id)
    if file_reference:
        data += _tl_bytes(file_reference)
    data += struct.pack('<qq', media_id, access_hash)
    data = _zero_encode(data) + bytes((PERSISTENT_ID_MINOR, PERSISTENT_ID_VERSION))
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def _session_path() -> Path:
    return Path(os.getenv('DATA_DIR', 'data')) / 'mtproto.session'


def _load_session() -> str:
    value = os.getenv('TELEGRAM_SESSION', '').strip()
    if value:
        return value
    try:
        return _session_path().read_text().strip()
    except OSError:
        return ''


def _save_session(value: str) -> None:
    if os.getenv('TELEGRAM_SESSION', '').strip() or not value:
        return
    path = _session_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(value)
        tmp.replace(path)
    except OSError as exc:
        LOG.info('MTProto session not saved: %s', type(exc).__name__)


async def _get_client(bot_token: str):
    global _client, _client_loop, _lock
    loop = asyncio.get_running_loop()
    if _lock is None or _client_loop is not loop:
        _lock, _client_loop, _client = asyncio.Lock(), loop, None
    async with _lock:
        if _client is not None and _client.is_connected():
            return _client
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        api_id, api_hash = api_credentials()
        client = TelegramClient(StringSession(_load_session()), api_id, api_hash,
                                receive_updates=False, request_retries=3, connection_retries=3,
                                flood_sleep_threshold=30, device_model='Veltrix Downloader',
                                app_version='10')
        # A saved session is reused; otherwise the bot token signs in once.
        await client.start(bot_token=bot_token)
        _save_session(client.session.save())
        _client = client
        LOG.info('MTProto upload session ready')
        return client


def _mime(kind: str, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if kind in {'video', 'animation'}:
        return 'video/mp4' if suffix in {'.mp4', '.m4v'} else mimetypes.guess_type(filename)[0] or 'video/mp4'
    if kind == 'audio':
        return {'.mp3': 'audio/mpeg', '.m4a': 'audio/mp4'}.get(suffix) or mimetypes.guess_type(filename)[0] or 'audio/mpeg'
    return mimetypes.guess_type(filename)[0] or 'application/octet-stream'


async def upload(path: Path, *, kind: str, bot_token: str, filename: str, byte_range=None,
                 duration: float = 0, width: int = 0, height: int = 0, title=None, performer=None,
                 thumb: Path | None = None, on_progress=None) -> str:
    """Upload one file (or byte range) and return its Bot API file_id."""
    from telethon.tl import functions, types
    from media_io import FileSlice

    if kind not in FILE_TYPES:
        raise ValueError('Unsupported large upload type')
    client = await _get_client(bot_token)
    size = byte_range[1] if byte_range else path.stat().st_size
    with path.open('rb') as handle:
        stream = FileSlice(handle, *byte_range) if byte_range else handle
        uploaded = await client.upload_file(stream, file_size=size, file_name=filename,
                                            part_size_kb=PART_SIZE_KB, progress_callback=on_progress)
    preview = None
    if thumb is not None and Path(thumb).is_file() and kind in {'video', 'animation'}:
        preview = await client.upload_file(str(thumb))
    attributes = [types.DocumentAttributeFilename(file_name=filename)]
    if kind in {'video', 'animation'}:
        attributes.insert(0, types.DocumentAttributeVideo(duration=float(duration or 0), w=int(width or 0),
                                                          h=int(height or 0), supports_streaming=True))
        if kind == 'animation':
            attributes.append(types.DocumentAttributeAnimated())
    elif kind == 'audio':
        attributes.insert(0, types.DocumentAttributeAudio(duration=int(duration or 0), title=title or None,
                                                          performer=performer or None))
    media = types.InputMediaUploadedDocument(file=uploaded, mime_type=_mime(kind, filename),
                                             attributes=attributes, thumb=preview,
                                             force_file=True if kind == 'document' else None)
    # Bots may associate an upload with "no chat"; the Bot API sends it next.
    result = await client(functions.messages.UploadMediaRequest(peer=types.InputPeerEmpty(), media=media))
    document = getattr(result, 'document', None)
    if not isinstance(document, types.Document):
        raise RuntimeError('Telegram did not return the uploaded document')
    LOG.info('MTProto upload stored: kind=%s bytes=%s', kind, size)
    return bot_file_id(kind, document.dc_id, document.id, document.access_hash, document.file_reference)


async def close() -> None:
    global _client
    client, _client = _client, None
    if client is not None:
        try:
            await client.disconnect()
        except Exception as exc:  # pragma: no cover - shutdown only
            LOG.info('MTProto disconnect: %s', type(exc).__name__)
