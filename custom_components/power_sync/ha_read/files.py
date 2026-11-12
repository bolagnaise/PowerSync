"""Bounded, descriptor-relative reads of HA-managed configuration and logs.

No constructors, includes, templates, URLs, diagnostics callbacks or commands
are executed. Protected storage is available only to named internal adapters.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .reader import ReadError, SECRET_KEY, sanitized

MAX_FILE = 1024 * 1024
MAX_LOG = 256 * 1024
PROTECTED = {"secrets.yaml", "secrets.yml", "credentials.yaml", "credentials.yml", "auth.yaml", "auth.yml"}


class ManagedFiles:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)

    def _parts(self, path, *, storage=False):
        if type(path) is not str or len(path) > 256 or "\\" in path or "\0" in path:
            raise ReadError("invalid_query")
        parts = PurePosixPath(path).parts
        if path.startswith("/") or any(p in {"..", "."} or p in PROTECTED for p in parts):
            raise ReadError("read_scope_denied")
        if any(p.startswith(".") for p in parts) and not (storage and len(parts) == 2 and parts[0] == ".storage"):
            raise ReadError("read_scope_denied")
        return parts

    def _open(self, parts):
        if not hasattr(os, "O_NOFOLLOW") or not os.supports_dir_fd or not parts:
            raise ReadError("reader_unavailable")
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)

    def read(self, path, *, storage=False, tail=False):
        parts = self._parts(path, storage=storage)
        try:
            descriptor = self._open(parts)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ReadError("read_scope_denied")
                cap = MAX_LOG if tail else MAX_FILE
                if tail and before.st_size > cap:
                    stream.seek(-cap, os.SEEK_END)
                raw = stream.read(cap + 1)
                after = os.fstat(stream.fileno())
                if len(raw) > cap:
                    raise ReadError("result_limit")
                if (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
                    raise ReadError("invalid_or_stale_cursor")
                return raw, {"size": before.st_size, "modified_ns": before.st_mtime_ns,
                             "tail_truncated": tail and before.st_size > cap}
        except ReadError:
            raise
        except OSError:
            raise ReadError("reader_unavailable") from None

    def configuration(self, path):
        parts = self._parts(path)
        if not parts or not parts[-1].endswith((".yaml", ".yml")):
            raise ReadError("read_scope_denied")
        raw, revision = self.read(path)
        return yaml_nodes(raw), revision

    def storage(self, key):
        # Exact typed storage classes, never an arbitrary .storage path reader.
        if key != "trace.saved_traces" and not re.fullmatch(r"lovelace(?:\.[a-z0-9_-]{1,64})?", key):
            raise ReadError("read_scope_denied")
        raw, revision = self.read(".storage/" + key, storage=True)
        try:
            value = json.loads(raw)
            if type(value) is not dict or type(value.get("data")) is not dict:
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            raise ReadError("reader_unavailable") from None
        return value["data"], revision


def yaml_nodes(raw):
    """Project YAML syntax without resolving !secret, !include or Python tags."""
    import yaml
    from yaml.nodes import MappingNode, ScalarNode, SequenceNode
    try:
        root = yaml.compose(raw, Loader=yaml.SafeLoader)
        active = set()
        count = [0]
        def visit(node, depth=0):
            count[0] += 1
            if count[0] > 20000 or depth > 20 or id(node) in active:
                raise ReadError("result_limit")
            if node is None:
                return None
            active.add(id(node))
            try:
                if node.tag == "!secret":
                    return {"redacted": True, "source": "secret_reference"}
                if node.tag.startswith("!include"):
                    return {"include_reference": node.value, "resolved": False}
                allowed = {"map", "seq", "str", "int", "float", "timestamp", "null", "bool"}
                if node.tag not in {"tag:yaml.org,2002:" + kind for kind in allowed}:
                    return {"unavailable": True, "reason": "unsupported_yaml_tag"}
                if isinstance(node, MappingNode):
                    result = {}
                    for key, value in node.value:
                        if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str" or key.value in result:
                            raise ReadError("reader_unavailable")
                        result[key.value] = {"redacted": True} if SECRET_KEY.search(key.value) else visit(value, depth + 1)
                    return result
                if isinstance(node, SequenceNode):
                    return [visit(value, depth + 1) for value in node.value]
                if isinstance(node, ScalarNode):
                    if len(node.value) > 32768:
                        raise ReadError("result_limit")
                    kind = node.tag.rsplit(":", 1)[-1]
                    if kind == "null":
                        return None
                    if kind == "bool":
                        return node.value.lower() in {"true", "yes", "on"}
                    # Preserve numeric spelling and templates as source text.
                    # No YAML constructor or expression evaluation is needed.
                    if kind in {"str", "int", "float", "timestamp"}:
                        return node.value
                    return {"unavailable": True, "reason": "unsupported_yaml_tag"}
                raise ReadError("reader_unavailable")
            finally:
                active.discard(id(node))
        return visit(root)
    except ReadError:
        raise
    except (yaml.YAMLError, UnicodeError, ValueError, TypeError, RecursionError):
        raise ReadError("reader_unavailable") from None


class Redactor:
    """Known-field/value redaction plus credential-shaped free-text patterns.

    This is a bounded projection, not a claim to recognise every possible secret
    embedded in arbitrary prose. The owner disclosure identifies that boundary.
    """
    def __init__(self, values=()):
        self.secrets = set()
        self.bytes = 0
        self.redactions = 0
        def collect(value, key="", depth=0):
            if depth > 20:
                raise ReadError("result_limit")
            if type(value) is str and SECRET_KEY.search(key) and len(value) >= 4:
                if value not in self.secrets:
                    self.bytes += len(value.encode())
                    if self.bytes > 65536 or len(self.secrets) >= 4096:
                        raise ReadError("result_limit")
                    self.secrets.add(value)
            elif type(value) is dict:
                for name, child in value.items():
                    if type(name) is str:
                        collect(child, name, depth + 1)
            elif type(value) in (list, tuple):
                for child in value:
                    collect(child, key, depth + 1)
        for value in values:
            collect(value)
        self.pattern = re.compile("|".join(re.escape(v) for v in sorted(self.secrets, key=len, reverse=True))) if self.secrets else None

    def text(self, value):
        def masked(match):
            self.redactions += 1
            return "[redacted]"
        if self.pattern:
            value = self.pattern.sub(masked, value)
        value = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", masked, value)
        value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9_.~+/-]+=*", masked, value)
        value = re.sub(r"(?i)(?:password|passwd|secret|token|api[_-]?key|authorization|cookie)\s*['\"]?\s*[:=]\s*['\"]?[^\s,;}\]\"']+", masked, value)
        value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", masked, value)
        return value

    def project(self, value):
        # The source document can span multiple transport pages. Individual
        # pages are still capped by HAReadBackend; this is a bounded local copy.
        value = sanitized(value, budget=[4 * MAX_FILE])
        def clean(item):
            if type(item) is str:
                return self.text(item)
            if type(item) is list:
                return [clean(v) for v in item]
            if type(item) is dict:
                if item.get("redacted") is True:
                    self.redactions += 1
                return {key: clean(v) for key, v in item.items()}
            return item
        return clean(value)
