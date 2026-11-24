#!/usr/bin/env python3
"""Raise the extractor minimum versions in requirements.txt to the newest stable releases.

YouTube and Instagram change often and yt-dlp/gallery-dl ship fixes within days.
Render installs requirements only when it builds the service, and a cached build
keeps an older release that still satisfies ">=". Raising the floor makes the
next deploy install the fixed release. The scheduled "Refresh extractors"
workflow runs this, tests the result and commits it to main.

Prints GitHub Actions outputs: ``changed=true|false`` and ``message=...``.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

# requirement prefix -> PyPI project
TRACKED = {'yt-dlp[default]': 'yt-dlp', 'gallery-dl': 'gallery-dl'}
STABLE = re.compile(r'\d+(?:\.\d+)+')


def as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split('.'))


def latest_stable(project: str, *, opener=urllib.request.urlopen) -> str | None:
    """The newest stable version PyPI lists for a project, or None."""
    with opener(f'https://pypi.org/pypi/{project}/json', timeout=30) as response:
        version = str(json.load(response).get('info', {}).get('version') or '')
    # Nightly/dev builds and pre-releases are never adopted automatically.
    return version if STABLE.fullmatch(version) else None


def bump(text: str, latest: dict[str, str]) -> tuple[str, list[str]]:
    """Raise each tracked ">=" floor that is older than ``latest[prefix]``."""
    changes = []
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        for prefix, version in latest.items():
            match = re.fullmatch(rf'({re.escape(prefix)}>=)(\d+(?:\.\d+)+)(\s*(?:#.*)?)(\r?\n)?', line)
            if not match or as_tuple(version) <= as_tuple(match[2]):
                continue
            lines[index] = f'{match[1]}{version}{match[3]}{match[4] or ""}'
            changes.append(f'{TRACKED[prefix]} {version}')
    return ''.join(lines), changes


def main(path: str = 'requirements.txt') -> int:
    requirements = Path(path)
    latest = {}
    for prefix, project in TRACKED.items():
        version = latest_stable(project)
        if version:
            latest[prefix] = version
    text, changes = bump(requirements.read_text(), latest)
    if changes:
        requirements.write_text(text)
    print(f'changed={"true" if changes else "false"}')
    print(f'message=Update {", ".join(changes)}' if changes else 'message=')
    return 0


if __name__ == '__main__':
    sys.exit(main(*sys.argv[1:]))
