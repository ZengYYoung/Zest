"""The portable part of a workspace; everything else in .zest stays local."""

from pathlib import PurePosixPath


DOCUMENT_SUFFIXES = {".md", ".rst", ".txt", ".json", ".yaml", ".yml"}
SHARED_IGNORE = ("# Shared project documents; all other Zest resources stay local\n"
                 "/*\n!/.gitignore\n!/docs/\n/docs/**\n!/docs/**/\n" +
                 "".join("!/docs/**/*" + "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c for c in suffix) + "\n"
                         for suffix in sorted(DOCUMENT_SUFFIXES)))
LEGACY_IGNORE = b"# Ignore all local Zest resources, including this file\n*\n"


def shared_document(path):
    parts = path.split("/")
    return (len(parts) >= 3 and parts[:2] == [".zest", "docs"]
            and PurePosixPath(path).suffix.casefold() in DOCUMENT_SUFFIXES)


def shared_directory(path):
    return path in (".zest", ".zest/docs") or path.startswith(".zest/docs/")


def legacy_document(path):
    """Documentation outside the managed home is readable, never edited by Zest."""
    value = PurePosixPath(path)
    parts = [part.casefold() for part in value.parts]
    return (not shared_document(path) and
            (value.suffix.casefold() in {".md", ".rst"}
             or any(part in {"doc", "docs"} for part in parts[:-1])
             or value.stem.casefold() in {"readme", "openapi", "swagger"}))
