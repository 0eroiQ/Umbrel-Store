"""Fresh-install preparation for Orbit's writable media library."""

from __future__ import annotations

import os
import re
import sys
import tempfile


DEFAULT_LIBRARY_ROOT = "/downloads/vortexo"
DEFAULT_MOVIES_DIR = f"{DEFAULT_LIBRARY_ROOT}/Movies"
DEFAULT_TV_DIR = f"{DEFAULT_LIBRARY_ROOT}/TV"


def _is_safe_library_path(path: str, library_root: str) -> bool:
    """Only create descendants of the explicitly configured library root."""
    candidate = os.path.realpath(os.path.abspath(path))
    root = os.path.realpath(os.path.abspath(library_root))
    if root == os.path.sep or candidate == root:
        return False
    try:
        return os.path.commonpath((candidate, root)) == root
    except ValueError:
        return False


def prepare_media_directories(
    movies_dir: str,
    tv_dir: str,
    library_root: str,
) -> list[str]:
    """Create missing movie/TV roots without writing outside the library bind."""
    paths = (movies_dir, tv_dir)
    for path in paths:
        if not _is_safe_library_path(path, library_root):
            raise ValueError(
                f"Refusing to create media directory outside {library_root}: {path}"
            )

    prepared = []
    for path in paths:
        os.makedirs(path, exist_ok=True)
        if not os.path.isdir(path):
            raise OSError(f"Media library path is not a directory: {path}")
        prepared.append(path)
    return prepared


_EMPTY_SECTION_PATTERN = re.compile(
    r"(?P<indent>^[ \t]+)if len\(section_response\) == 0:\r?\n"
    r"(?P=indent)[ \t]+ui_print\((?P<message>[^\n]+)\)\r?\n"
    r"(?P=indent)[ \t]+list_ = \[\]\r?\n"
    r"(?P=indent)[ \t]+break\r?\n"
    r"(?P=indent)else:\r?\n"
    r"(?P=indent)[ \t]+list_ \+= section_response",
    re.MULTILINE,
)


def patch_legacy_plex_source(source: str) -> tuple[str, bool]:
    """Make plex_debrid aggregate independent, possibly-empty sections.

    The bundled scanner used to discard all previously scanned media and stop
    when any selected Plex section was empty. This made Movies-only and
    Series-only installations look completely empty.
    """
    marker = "orbit-patched: empty Plex sections are valid"
    if marker in source:
        return source, False

    match = _EMPTY_SECTION_PATTERN.search(source)
    if match is None:
        raise RuntimeError("Bundled Plex scanner no longer matches the empty-section guard")

    indent = match.group("indent")
    nested = indent + "    "
    replacement = (
        f"{indent}if len(section_response) == 0:\n"
        f"{nested}ui_print(\"[plex] library section is empty; continuing with other sections.\")\n"
        f"{nested}continue  # {marker}\n"
        f"{indent}list_ += section_response"
    )
    patched = _EMPTY_SECTION_PATTERN.sub(replacement, source, count=1)

    patched = patched.replace(
        "[plex error]: Your library seems empty. To prevent unwanted behaviour, "
        "no further downloads will be started. If your library really is empty, "
        "please add at least one media item manually.",
        "[plex] selected library sections are empty; the library is ready for its first item.",
    )
    return patched, True


def patch_legacy_plex_file(path: str) -> bool:
    """Atomically apply the empty-section compatibility fix to plex_debrid."""
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    patched, changed = patch_legacy_plex_source(source)
    if not changed:
        return False

    directory = os.path.dirname(path) or "."
    descriptor, temporary = tempfile.mkstemp(prefix=".orbit-plex-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(patched)
        os.chmod(temporary, os.stat(path).st_mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def main() -> int:
    library_root = os.environ.get("ORBIT_LIBRARY_ROOT", DEFAULT_LIBRARY_ROOT)
    movies_dir = os.environ.get(
        "ORBIT_MOVIES_DIR",
        os.environ.get("PD_LIBRARY_MOVIES_DIR", DEFAULT_MOVIES_DIR),
    )
    tv_dir = os.environ.get(
        "ORBIT_TV_DIR",
        os.environ.get("PD_LIBRARY_TV_DIR", DEFAULT_TV_DIR),
    )
    try:
        prepare_media_directories(movies_dir, tv_dir, library_root)
    except (OSError, ValueError) as error:
        print(f"[orbit] media directory preparation skipped: {error}", file=sys.stderr)

    plex_path = os.path.join(
        os.environ.get("PD_ROOT", "/app/plex_debrid"),
        "content",
        "services",
        "plex.py",
    )
    if os.path.isfile(plex_path):
        try:
            patch_legacy_plex_file(plex_path)
        except (OSError, RuntimeError) as error:
            print(f"[orbit] could not prepare bundled Plex scanner: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
