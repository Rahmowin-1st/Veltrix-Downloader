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


def _flag_values(data: bytes, domains: set[str], names: set[str]) -> dict[str, str]:
    """Values of non-secret flag cookies only (e.g. Pinterest's _auth=0/1)."""
    values = {}
    for raw in data.decode('utf-8', 'replace').splitlines():
        fields = raw.removeprefix('#HttpOnly_').split('\t')
        if len(fields) == 7 and fields[0].strip().lower().lstrip('.') in domains and fields[5] in names:
            values[fields[5]] = fields[6].strip()
    return values


def signed_in(platform: str, data: bytes, *, now: float | None = None) -> bool | None:
    """Whether a cookie file holds a signed-in session; None when it cannot be told."""
    platform = platform.upper()
    now = time.time() if now is None else now
    if platform == 'YOUTUBE':
        return youtube_signed_in(data, now=now)

    def names(domains: set[str]) -> set[str]:
        return {name for domain, name, expiry in _cookie_rows(data)
                if domain in domains and (not expiry or expiry > now)}

    if platform == 'INSTAGRAM':
        # sessionid exists only for a signed-in Instagram session.
        return 'sessionid' in names({'instagram.com', 'www.instagram.com'})
    if platform == 'PINTEREST':
        domains = {'pinterest.com', 'www.pinterest.com'}
        # Visitors also get _pinterest_sess; _auth is 1 only when signed in.
        return ('_pinterest_sess' in names(domains)
                and _flag_values(data, domains, {'_auth'}).get('_auth') == '1')
    return None


def cookie_state(platform: str, path: str | None = None) -> str:
    """'missing', 'signed-out', 'signed-in' or 'present' (state unknown) for a platform."""
    raw = os.getenv(f'{platform.upper()}_COOKIE_FILE', '').strip() if path is None else path
    if not raw:
        return 'missing'
    try:
        source = Path(raw)
        if not source.is_file() or source.stat().st_size > MAX_COOKIE_BYTES:
            return 'missing'
        data = source.read_bytes()
    except OSError:
        return 'missing'
    state = signed_in(platform, data)
    return 'present' if state is None else 'signed-in' if state else 'signed-out'


def youtube_cookie_state(path: str | None = None) -> str:
    """'missing', 'signed-out' or 'signed-in' for the configured YouTube cookies."""
    return cookie_state('YOUTUBE', path)


def pace_account(platform: str, admitted: Path, spacing: float, directory: Path, *,
                 clock=time.time, sleep=time.sleep) -> float:
    """Space extractions that use the owner's account, across worker processes.

    A platform sees at most one new signed-in job every ``spacing`` seconds, like
    a person opening links one after another. ``admitted`` is a marker file in
    the job's own folder: once it exists, later extractors of that job (fallback
    routes) do not wait again, whatever other jobs ran meanwhile. Returns the
    seconds waited.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX hosts run one job at a time
        return 0.0
    import json

    if spacing <= 0 or admitted.exists():
        return 0.0
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / f'{platform.lower()}.json', 'a+', encoding='utf-8') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        try:
            last = json.loads(handle.read() or '{}')
        except ValueError:
            last = {}
        try:
            waited = max(0.0, min(spacing, spacing - (clock() - float(last.get('at') or 0))))
        except (TypeError, ValueError):
            waited = 0.0
        if waited:
            sleep(waited)
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({'at': clock()}))
        handle.flush()
    try:
        admitted.parent.mkdir(parents=True, exist_ok=True)
        admitted.touch()
    except OSError:
        pass
    return waited


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
    for name in PLATFORMS:
        state = cookie_state(name)
        label = name.capitalize().replace('Youtube', 'YouTube')
        if state == 'signed-in':
            log.info('%s cookies: signed-in session found', label)
        elif state == 'signed-out':
            log.warning('%s cookies contain no signed-in session (missing or expired session cookies). '
                        'Export them while signed in.', label)
    return paths
