"""Materialize optional, owner-authorized source cookies on ephemeral hosts."""
from __future__ import annotations

import base64
import logging
import os
import tempfile
from pathlib import Path


PLATFORMS = ('YOUTUBE', 'INSTAGRAM', 'PINTEREST', 'SNAPCHAT')
_HEADERS = (b'# Netscape HTTP Cookie File', b'# HTTP Cookie File')
# Render "Secret Files" are mounted read-only here, e.g. /etc/secrets/youtube_cookies.txt.
SECRET_DIR = Path(os.getenv('SECRET_FILES_DIR', '/etc/secrets'))
MAX_COOKIE_BYTES = 2 * 1024 * 1024
log = logging.getLogger('veltrix.source_auth')


def _secret_file(name: str) -> Path | None:
    """A cookie file the extractor cannot update in place (read-only secret mount)."""
    configured = os.getenv(f'{name}_COOKIE_FILE', '').strip()
    candidates = [Path(configured)] if configured else []
    candidates.append(SECRET_DIR / f'{name.lower()}_cookies.txt')
    for path in candidates:
        try:
            if not path.is_file():
                continue
            # A writable owner file (e.g. on Termux) is used where it is.
            if path.resolve().is_relative_to(SECRET_DIR.resolve()) or not os.access(path, os.W_OK):
                return path
        except OSError:
            continue
    return None


def _payload(name: str) -> bytes | None:
    raw = os.getenv(f'{name}_COOKIES_B64', '').strip()
    if raw:
        try:
            return base64.b64decode(raw, validate=True)
        except ValueError:
            log.warning('%s_COOKIES_B64 is invalid; source authentication skipped', name)
            return None
    path = _secret_file(name)
    if path is None:
        return None
    try:
        if path.stat().st_size > MAX_COOKIE_BYTES:
            log.warning('%s cookie secret file is too large; source authentication skipped', name)
            return None
        return path.read_bytes()
    except OSError as exc:
        log.warning('%s cookie secret file unreadable: %s', name, type(exc).__name__)
        return None


def materialize_source_cookies(directory: Path = Path('/tmp')) -> dict[str, Path]:
    """Pass Netscape cookie files to supported extractors without logging secrets.

    Sources, in order: ``<PLATFORM>_COOKIES_B64``, then a read-only secret file
    (``<PLATFORM>_COOKIE_FILE`` or ``/etc/secrets/<platform>_cookies.txt``). Each
    becomes a private, writable copy because yt-dlp saves refreshed cookies.
    """
    paths = {}
    for name in PLATFORMS:
        payload = _payload(name)
        if payload is None:
            continue
        if (not payload or len(payload) > MAX_COOKIE_BYTES or
                not payload.lstrip(b'\xef\xbb\xbf').splitlines()[0].startswith(_HEADERS)):
            log.warning('%s cookies are not a Netscape cookie file; source authentication skipped', name)
            continue
        directory.mkdir(parents=True, exist_ok=True)
        fd, name_on_disk = tempfile.mkstemp(prefix=f'veltrix-{name.lower()}-',
                                            suffix='.cookies', dir=directory)
        path = Path(name_on_disk)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
            path.chmod(0o600)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        os.environ[f'{name}_COOKIE_FILE'] = str(path)
        paths[name] = path
    return paths
