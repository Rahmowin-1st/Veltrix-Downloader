"""Materialize optional, owner-authorized source cookies on ephemeral hosts."""
from __future__ import annotations

import base64
import logging
import os
import tempfile
from pathlib import Path


PLATFORMS = ('YOUTUBE', 'INSTAGRAM', 'PINTEREST', 'SNAPCHAT')
_HEADERS = (b'# Netscape HTTP Cookie File', b'# HTTP Cookie File')
log = logging.getLogger('veltrix.source_auth')


def materialize_source_cookies(directory: Path = Path('/tmp')) -> dict[str, Path]:
    """Pass Netscape cookie files to supported extractors without logging secrets."""
    paths = {}
    for name in PLATFORMS:
        raw = os.getenv(f'{name}_COOKIES_B64', '').strip()
        if not raw:
            continue
        try:
            payload = base64.b64decode(raw, validate=True)
        except ValueError:
            log.warning('%s_COOKIES_B64 is invalid; source authentication skipped', name)
            continue
        if (not payload or len(payload) > 2 * 1024 * 1024 or
                not payload.lstrip(b'\xef\xbb\xbf').splitlines()[0].startswith(_HEADERS)):
            log.warning('%s_COOKIES_B64 is not a Netscape cookie file; source authentication skipped', name)
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
