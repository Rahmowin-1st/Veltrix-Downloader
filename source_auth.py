"""Materialize optional, owner-authorized source cookies on ephemeral hosts."""
from __future__ import annotations

import base64
import logging
import os
import tempfile
import time
from pathlib import Path


PLATFORMS = ('YOUTUBE', 'INSTAGRAM', 'PINTEREST', 'SNAPCHAT')
_HEADERS = (b'# Netscape HTTP Cookie File', b'# HTTP Cookie File')
# Render "Secret Files" are mounted read-only here, e.g. /etc/secrets/youtube_cookies.txt.
SECRET_DIR = Path(os.getenv('SECRET_FILES_DIR', '/etc/secrets'))
MAX_COOKIE_BYTES = 2 * 1024 * 1024
# yt-dlp treats a YouTube session as signed in when LOGIN_INFO and one of
# these SAPISID cookies apply to www.youtube.com.
_YOUTUBE_DOMAINS = {'youtube.com', 'www.youtube.com'}
_YOUTUBE_SID = {'SAPISID', '__Secure-1PAPISID', '__Secure-3PAPISID'}
log = logging.getLogger('veltrix.source_auth')


def _cookie_rows(data: bytes) -> list[tuple[str, str, float]]:
    """(domain, name, expiry) of each Netscape cookie line. Values are never kept."""
    rows = []
    for raw in data.decode('utf-8', 'replace').splitlines():
        line = raw.removeprefix('#HttpOnly_')
        if not line.strip() or line.startswith('#'):
            continue
        fields = line.split('\t')
        if len(fields) != 7:
            continue
        try:
            expiry = float(fields[4] or 0)
        except ValueError:
            expiry = 0.0
        rows.append((fields[0].strip().lower().lstrip('.'), fields[5], expiry))
    return rows


def youtube_signed_in(data: bytes, *, now: float | None = None) -> bool:
    """Whether a cookie file carries an unexpired signed-in YouTube session."""
    now = time.time() if now is None else now
    names = {name for domain, name, expiry in _cookie_rows(data)
             if domain in _YOUTUBE_DOMAINS and (not expiry or expiry > now)}
    return 'LOGIN_INFO' in names and bool(names & _YOUTUBE_SID)


def youtube_cookie_state(path: str | None = None) -> str:
    """'missing', 'signed-out' or 'signed-in' for the configured YouTube cookies."""
    raw = os.getenv('YOUTUBE_COOKIE_FILE', '').strip() if path is None else path
    if not raw:
        return 'missing'
    try:
        source = Path(raw)
        if not source.is_file() or source.stat().st_size > MAX_COOKIE_BYTES:
            return 'missing'
        data = source.read_bytes()
    except OSError:
        return 'missing'
    return 'signed-in' if youtube_signed_in(data) else 'signed-out'


def job_cookie_copy(platform: str, folder: str | Path) -> Path | None:
    """A private copy of the platform's cookies for one extraction.

    yt-dlp and gallery-dl rewrite their cookie file in place when they finish.
    Two concurrent jobs sharing one file could read it half-written and lose the
    owner's sign-in, so each job works on its own copy.
    """
    shared = os.getenv(f'{platform.upper()}_COOKIE_FILE', '').strip()
    if not shared:
        return None
    try:
        source = Path(shared)
        if not source.is_file() or source.stat().st_size > MAX_COOKIE_BYTES:
            return None
        data = source.read_bytes()
    except OSError:
        return None
    directory = Path(folder)
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f'{platform.lower()}-', suffix='.cookies', dir=directory)
    copy = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        copy.chmod(0o600)
    except BaseException:
        copy.unlink(missing_ok=True)
        raise
    return copy


def keep_job_cookies(platform: str, copy: Path | None) -> bool:
    """Atomically save cookies an extractor refreshed, unless any were lost.

    A refreshed copy may change values or add cookies. It never replaces the
    shared file when it dropped a cookie the owner supplied (for example a
    signed-out session), and a read-only secret file is never modified.
    """
    shared = os.getenv(f'{platform.upper()}_COOKIE_FILE', '').strip()
    if copy is None or not shared:
        return False
    target = Path(shared)
    try:
        refreshed = copy.read_bytes()
        original = target.read_bytes()
        if (target.resolve().is_relative_to(SECRET_DIR.resolve())
                or not os.access(target, os.W_OK) or not os.access(target.parent, os.W_OK)):
            return False
    except OSError:
        return False
    if refreshed == original or len(refreshed) > MAX_COOKIE_BYTES:
        return False
    if not refreshed.lstrip(b'\xef\xbb\xbf').startswith(_HEADERS):
        return False
    kept = {(domain, name) for domain, name, _ in _cookie_rows(refreshed)}
    now = time.time()
    # Extractors drop cookies that have expired on their own; that is no loss.
    required = {(domain, name) for domain, name, expiry in _cookie_rows(original) if not expiry or expiry > now}
    if not kept or not required <= kept:
        return False
    fd, name = tempfile.mkstemp(prefix=f'.{target.name}.', suffix='.tmp', dir=target.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(refreshed)
        staged.chmod(0o600)
        os.replace(staged, target)
    except OSError:
        staged.unlink(missing_ok=True)
        return False
    return True


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
    state = youtube_cookie_state()
    if state == 'signed-in':
        log.info('YouTube cookies: signed-in session found')
    elif state == 'signed-out':
        log.warning('YouTube cookies contain no signed-in session (LOGIN_INFO/SAPISID missing or expired). '
                    'Export them while signed in on youtube.com.')
    return paths
