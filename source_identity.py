"""Source-derived physical verification; no logical JSON, acquisition or writes."""
import hashlib
import os
import pathlib
import re
import stat


def backing_path(root, relative_path, content_identity):
    return _backing_path(pathlib.Path(root).resolve(), relative_path, content_identity)


def _backing_path(resolved_root, relative_path, content_identity):
    relative = pathlib.PurePosixPath(relative_path)
    if (relative.is_absolute() or '..' in relative.parts or '\\' in relative_path
            or '#' in relative_path or not relative.parts):
        raise ValueError('Invalid source relative path')
    if not re.fullmatch(r'[a-f0-9]{64}', content_identity):
        raise ValueError('Invalid physical source hash')
    path = resolved_root / pathlib.Path(relative)
    if not path.resolve().is_relative_to(resolved_root):
        raise ValueError('Source backing escapes the replica')
    return path


class SourceVerifier:
    def __init__(self, root):
        self.root = pathlib.Path(root).resolve()
        self.verified_files = set()

    def verify(self, relative_path, content_identity, expected_size=None):
        path = _backing_path(self.root, relative_path, content_identity)
        # New hardening: don't block opening a FIFO before checking its type.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('Source is not a regular file')
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                    size += len(block)
                    digest.update(block)
        finally:
            os.close(fd)
        if expected_size is not None and size != expected_size:
            raise ValueError('Source size mismatch')
        if digest.hexdigest() != content_identity:
            raise ValueError('Source hash mismatch')
        self.verified_files.add(path.relative_to(self.root).as_posix())
        return {'kind': 'file', 'relative_path': relative_path,
                'file_sha256': content_identity, 'size': size}
