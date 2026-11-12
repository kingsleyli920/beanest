"""Download a verified file revision without exporting private room metadata."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile

from .project_catalog import ProjectCatalogError, validate_text_path
from .folder_workspace import FORMAT as FOLDER_FORMAT, MAX_CHANGED_TOTAL_BYTES, MAX_SOURCE_FILES, validate_folder_path
from .project_workspace import ProjectWorkspaceError


MAX_FILES = 16
MAX_BYTES = 64 * 1024
MANIFEST_NAME = ".beanest-export.json"


class ArtifactExportError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def export_project(project: dict, *, expected_revision: int, binaries=None) -> dict:
    """Accept an already-opened immutable workspace snapshot, not host paths.

    Both digests are returned: snapshot_sha256 binds the input revision, while
    sha256 binds the exact ZIP bytes. The archive contains only the supplied
    files plus a small public manifest, never sessions, messages or source paths.
    A folder snapshot's binary file (content None) takes its exact bytes from
    binaries[path]; captured files a partner deleted are named in the manifest.
    """
    if type(expected_revision) is not int or expected_revision < 0:
        raise ArtifactExportError("invalid_project_revision")
    if not isinstance(project, dict) or project.get("revision") != expected_revision:
        raise ArtifactExportError("project_revision_conflict")
    files = project.get("files")
    folder = project.get("format") == FOLDER_FORMAT
    file_limit, byte_limit = (MAX_SOURCE_FILES, MAX_CHANGED_TOTAL_BYTES) if folder else (MAX_FILES, MAX_BYTES)
    deleted = project.get("deleted_files", []) if folder else []
    if (not isinstance(files, list) or not isinstance(deleted, list)
            or not 1 <= len(files) + len(deleted) or len(files) > file_limit):
        raise ArtifactExportError("project_export_unavailable")
    prepared, seen, size = [], set(), 0
    for file in files:
        if not isinstance(file, dict):
            raise ArtifactExportError("project_export_corrupt")
        try:
            path = (validate_folder_path if folder else validate_text_path)(file.get("path"))
            if folder and file.get("text") is False and file.get("content") is None:
                content = (binaries or {})[path]
                if not isinstance(content, bytes):
                    raise TypeError
            else:
                content = file["content"].encode("utf-8")
                if "\0" in file["content"]:
                    raise ArtifactExportError("project_export_corrupt")
        except (ProjectCatalogError, ProjectWorkspaceError, KeyError, AttributeError, TypeError, UnicodeError):
            raise ArtifactExportError("project_export_corrupt") from None
        if path.casefold() in seen or path.casefold() == MANIFEST_NAME.casefold():
            raise ArtifactExportError("project_export_corrupt")
        seen.add(path.casefold())
        size += len(content)
        if size > byte_limit:
            raise ArtifactExportError("project_limit_exceeded")
        digest = hashlib.sha256(content).hexdigest()
        if digest != file.get("sha256"):
            raise ArtifactExportError("project_export_corrupt")
        prepared.append((path, content, digest))
    try:
        snapshot_bytes = json.dumps(project, sort_keys=True, ensure_ascii=True,
                                    separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise ArtifactExportError("project_export_corrupt") from None
    snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
    manifest = {"format": "beanest-export-v1", "revision": expected_revision,
                "snapshot_sha256": snapshot_digest,
                "files": [{"path": path, "sha256": digest} for path, _, digest in sorted(prepared)]}
    if folder:
        manifest["scope"] = "changed_files_only"
    if deleted:
        try:
            manifest["deleted"] = sorted(validate_folder_path(file["path"]) for file in deleted)
        except (ProjectWorkspaceError, KeyError, TypeError):
            raise ArtifactExportError("project_export_corrupt") from None
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        entries = [(path, content) for path, content, _ in sorted(prepared)]
        entries.append((MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2).encode()))
        for path, content in entries:
            entry = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100600 << 16
            archive.writestr(entry, content)
    raw = buffer.getvalue()
    return {"filename": f"beanest-revision-{expected_revision}.zip", "content_type": "application/zip",
            "content_base64": base64.b64encode(raw).decode("ascii"), "revision": expected_revision,
            "snapshot_sha256": snapshot_digest, "sha256": hashlib.sha256(raw).hexdigest(),
            "files_count": len(prepared)}
