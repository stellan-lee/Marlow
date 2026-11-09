"""Deterministic file operations executed ONLY inside app-server command/exec.

The host sends this source to the sandboxed Python interpreter. No host imports,
provider client or inference loop are available in this worker.
"""

import codecs
import fnmatch
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import types

MAX_BYTES = 10 * 1024 * 1024
OUTPUT_CHARS = 128 * 1024
mutating = False


def read_text(path):
    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_BYTES:
        raise ValueError(
            "File is not a regular text file or exceeds the 10 MiB patch limit"
        )
    data = path.read_bytes()
    bom = data.startswith(codecs.BOM_UTF8)
    text = data.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), bom, newline


def atomic_write(path, text):
    global mutating
    bom, newline = False, "\n"
    if path.exists():
        _, bom, newline = read_text(path)
    data = text.replace("\r\n", "\n").replace("\n", newline).encode("utf-8")
    if bom:
        data = codecs.BOM_UTF8 + data
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".marlow-write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        mutating = True
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def execute(name, path, args):
    global mutating
    if name == "vision_analyze":
        import base64

        if (
            not stat.S_ISREG(path.stat().st_mode)
            or path.stat().st_size > 4 * 1024 * 1024
        ):
            raise ValueError("Images must be regular files of at most 4 MiB")
        raw = path.read_bytes()
        mime = (
            "image/png"
            if raw.startswith(b"\x89PNG\r\n\x1a\n")
            else "image/jpeg"
            if raw.startswith(b"\xff\xd8\xff")
            else "image/gif"
            if raw.startswith((b"GIF87a", b"GIF89a"))
            else "image/webp"
            if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
            else None
        )
        if mime is None:
            raise ValueError("Unsupported image format; use PNG, JPEG, GIF or WebP")
        return {
            "_multimodal": True,
            "content": [
                {
                    "type": "text",
                    "text": args.get("question") or "Image requested by the user",
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:"
                        + mime
                        + ";base64,"
                        + base64.b64encode(raw).decode()
                    },
                },
            ],
        }
    if name == "read_file":
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("Only regular files can be read")
        offset = max(1, int(args.get("offset") or 1)) - 1
        count = min(max(int(args.get("limit") or 500), 1), 2000)
        rows, remaining = [], OUTPUT_CHARS
        with path.open(encoding="utf-8-sig") as stream:
            for index in range(offset + count):
                line = stream.readline(OUTPUT_CHARS + 1)
                if not line:
                    break
                if len(line) > OUTPUT_CHARS:
                    raise ValueError("Line exceeds bounded text read size")
                if index >= offset:
                    rows.append(line[:remaining])
                    remaining -= len(rows[-1])
                    if remaining <= 0:
                        break
        return "".join(rows)
    if name == "write_file":
        atomic_write(path, args["content"])
        return "File written"
    if name == "patch" and args.get("mode", "replace") == "replace":
        content, _, _ = read_text(path)
        old, new = args["old_string"], args["new_string"]
        if (
            not old
            or old not in content
            or (not args.get("replace_all") and content.count(old) != 1)
        ):
            raise ValueError("Replacement must identify existing text unambiguously")
        atomic_write(
            path, content.replace(old, new, -1 if args.get("replace_all") else 1)
        )
        return "File patched"
    if name == "patch" and args.get("mode") == "patch":
        # Reuse Marlow's deterministic V4A parser and matching rules. Supply
        # trusted installed source explicitly; never import project modules.
        package = types.ModuleType("tools")
        package.__path__ = []
        sys.modules["tools"] = package
        for module_name, source in args.pop("_parser_sources").items():
            module = types.ModuleType(module_name)
            sys.modules[module_name] = module
            exec(compile(source, module_name, "exec"), module.__dict__)
        parser = sys.modules["tools.patch_parser"]
        operations, error = parser.parse_v4a_patch(args["patch"])
        if error:
            raise ValueError(error)
        paths = args.pop("_authorized_patch_paths")
        for operation in operations:
            operation.file_path = paths[operation.file_path]
            if operation.new_path:
                operation.new_path = paths[operation.new_path]

        # Stage transformations in memory before any change reaches disk.
        class Files:
            def __init__(self):
                self.staged = {}

            def read_file_raw(self, raw):
                try:
                    content = (
                        self.staged[raw]
                        if raw in self.staged
                        else read_text(Path(raw))[0]
                    )
                    if content is None:
                        raise FileNotFoundError(raw)
                    return types.SimpleNamespace(content=content, error=None)
                except (OSError, ValueError) as exc:
                    return types.SimpleNamespace(content="", error=str(exc))

            def write_file(self, raw, content):
                self.staged[raw] = content
                return types.SimpleNamespace(error=None)

            def delete_file(self, raw):
                self.staged[raw] = None
                return types.SimpleNamespace(error=None)

            def move_file(self, raw, destination):
                self.staged[destination] = self.read_file_raw(raw).content
                self.staged[raw] = None
                return types.SimpleNamespace(error=None)

        files = Files()
        errors = parser._validate_operations(operations, files)
        if errors:
            raise ValueError("\n".join(errors))
        for operation in operations:
            operation_type = operation.operation.value
            success, detail = getattr(parser, "_apply_" + operation_type)(
                operation, files
            )
            if not success:
                raise ValueError(detail)
        for raw, content in files.staged.items():
            target = Path(raw)
            if content is None:
                mutating = True
                target.unlink()
            else:
                atomic_write(target, content)
        return "Patch applied: " + ", ".join(files.staged)
    if name == "search_files":
        pattern = args["pattern"]
        limit = min(max(int(args.get("limit") or 50), 1), 2000)
        rows, output_size = [], 0
        paths = [path] if path.is_file() else path.rglob(args.get("file_glob") or "*")
        for item in paths:
            if item.is_symlink() or not item.is_file():
                continue
            if args.get("target") == "files":
                matches = [str(item)] if fnmatch.fnmatch(item.name, pattern) else []
            else:
                matches = []
                try:
                    if item.stat().st_size > MAX_BYTES:
                        continue
                    with item.open(encoding="utf-8") as stream:
                        for index, line in enumerate(stream, 1):
                            if re.search(pattern, line):
                                matches.append(f"{item}:{index}:{line[:2000].rstrip()}")
                            if len(matches) >= limit:
                                break
                except (OSError, UnicodeError):
                    continue
            for match in matches:
                rows.append(match)
                output_size += len(match)
                if len(rows) >= limit or output_size >= OUTPUT_CHARS:
                    return "\n".join(rows)[:OUTPUT_CHARS]
        return "\n".join(rows)[:OUTPUT_CHARS]
    raise ValueError("Unsupported file operation")


if __name__ == "__main__":
    try:
        name, raw_path, encoded = sys.argv[1:]
        result = execute(name, Path(raw_path), json.loads(encoded))
        print(
            json.dumps({
                "success": True,
                "result": result,
                "write_state": "completed" if mutating else "not_started",
            })
        )
    except Exception as exc:
        print(
            json.dumps({
                "success": False,
                "error": str(exc),
                "write_state": "may_have_written" if mutating else "not_started",
            })
        )
