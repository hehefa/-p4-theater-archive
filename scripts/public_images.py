"""Offline checks for approved Git images and independently read-back Releases.

The publisher supplies genuine GitHub observations. This module checks their
cross-consistency; it does not fetch, authenticate or download a Release.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import unicodedata
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile
import zlib

HEX = re.compile(r"[a-f0-9]{64}\Z")
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}\Z")
IMAGE_MIMES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml"}
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _hash(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def _positive(value):
    return type(value) is int and value > 0


def _relative(value):
    _require(isinstance(value, str) and value and not any(ord(c) < 32 or ord(c) == 127 for c in value), "Invalid public file path")
    _require(not any(c in value for c in "\\:%?#") and not PurePosixPath(value).is_absolute(), "Unsafe public file path")
    parts = value.split("/")
    _require(all(x not in ("", ".", "..") and x == x.rstrip(" .") for x in parts), "Noncanonical public file path")
    reserved = {"con", "prn", "aux", "nul", *("com" + str(n) for n in range(1, 10)), *("lpt" + str(n) for n in range(1, 10))}
    _require(all(x.split(".")[0].casefold() not in reserved for x in parts), "Reserved public file path")
    return parts


def _local(root, value):
    target = root
    for part in _relative(value):
        target = target / part
        _require(not target.is_symlink(), "Public files cannot use symlinks")
    _require(target.is_file(), "Registered public file is missing")
    return target


def _json(path):
    def pairs(rows):
        result = {}
        for key, value in rows:
            _require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result
    def finite(unused):
        raise ValueError("Non-finite JSON value")
    return json.loads(path.read_bytes().decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=finite)


def _timestamp(value):
    _require(isinstance(value, str), "Receipt observation requires a timezone")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid receipt observation time") from None
    _require(stamp.utcoffset() is not None, "Receipt observation requires a timezone")


def _no_link(row):
    _require(all(row.get(k) is None or row[k] is False for k in ("symlink", "is_symlink")), "Symlink member is not allowed")
    _require(not row.get("link_target") and row.get("type") not in ("symlink", "hardlink"), "Link member is not allowed")
    mode = row.get("mode")
    if "external_attr" in row:
        _require(type(row["external_attr"]) is int, "Invalid archive member attributes")
        mode = row["external_attr"] >> 16
    if mode is not None:
        _require(type(mode) is int and stat.S_IFMT(mode) in (0, stat.S_IFREG), "Nonregular archive member is not allowed")


def _version(row):
    _require(isinstance(row, dict) and _hash(row.get("sha256")) and _positive(row.get("bytes")), "Invalid public file version")


def _checked_body(record, body):
    _version(record)
    _require(isinstance(body, bytes) and len(body) == record["bytes"] and hashlib.sha256(body).hexdigest() == record["sha256"], "Public file bytes or SHA-256 differ")


def _xml(body):
    try:
        text = body.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("Public XML must use UTF-8") from None
    _require(not re.search(r"<!\s*(?:DOCTYPE|ENTITY)|<\?xml-stylesheet", text, re.I), "DTD, entity or stylesheet instructions are not allowed")
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        raise ValueError("Invalid public XML") from None


def _svg(body):
    root = _xml(body)
    _require(root.tag in ("svg", "{http://www.w3.org/2000/svg}svg"), "SVG signature does not match MIME")
    active = {"script", "foreignobject", "iframe", "object", "embed", "audio", "video", "animate", "animatemotion", "animatetransform", "set"}
    for node in root.iter():
        _require(node.tag.rsplit("}", 1)[-1].casefold() not in active, "Active SVG content is not allowed")
        for name, value in node.attrib.items():
            name = name.rsplit("}", 1)[-1].casefold()
            _require(not name.startswith("on"), "SVG event handlers are not allowed")
            if name in ("href", "src"):
                _require(value.startswith("#") or re.fullmatch(r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+", value, re.I), "External or active SVG references are not allowed")
        style_attributes = {"style", "fill", "stroke", "filter", "clip-path", "mask", "cursor", "marker-start", "marker-mid", "marker-end"}
        _require(not any(name.rsplit("}", 1)[-1].casefold() == "base" for name in node.attrib), "SVG base URL is not allowed")
        css = " ".join([value for name, value in node.attrib.items() if name in style_attributes] +
                       [node.text or "" if node.tag.rsplit("}", 1)[-1].casefold() == "style" else ""])
        _require(not re.search(r"javascript\s*:|expression\s*\(|@import|behavior\s*:|\\", css, re.I), "Active SVG style is not allowed")
        static = re.sub(r"url\(\s*(['\"]?)#[A-Za-z0-9_.:-]+\1\s*\)", "", css, flags=re.I)
        _require(not re.search(r"url\s*\(", static, re.I), "External SVG style references are not allowed")
    def number(value):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)(?:px)?\s*", value or "")
        try:
            return int(float(match[1])) if match else 0
        except (ValueError, OverflowError):
            return 0
    width, height = number(root.get("width")), number(root.get("height"))
    if not (width and height):
        view = re.split(r"[ ,]+", root.get("viewBox", "").strip())
        try:
            if len(view) == 4: width, height = int(float(view[2])), int(float(view[3]))
        except (ValueError, OverflowError):
            raise ValueError("Invalid SVG dimensions") from None
    return width, height


def _png(body):
    _require(body.startswith(b"\x89PNG\r\n\x1a\n"), "PNG signature does not match MIME")
    offset, size, idat = 8, None, False
    while offset + 12 <= len(body):
        length = int.from_bytes(body[offset:offset + 4], "big")
        end = offset + 12 + length
        _require(end <= len(body), "Truncated PNG chunk")
        kind = body[offset + 4:offset + 8]; chunk = body[offset + 8:end - 4]
        _require(zlib.crc32(kind + chunk) & 0xffffffff == int.from_bytes(body[end - 4:end], "big"), "PNG CRC mismatch")
        if size is None:
            _require(kind == b"IHDR" and length == 13, "PNG requires its IHDR")
            size = struct.unpack(">II", chunk[:8])
        elif kind == b"IHDR":
            raise ValueError("Duplicate PNG IHDR")
        idat |= kind == b"IDAT"
        if kind == b"IEND":
            _require(length == 0 and end == len(body) and idat, "Invalid PNG ending")
            return size
        offset = end
    raise ValueError("Truncated PNG")


def _jpeg(body):
    _require(body.startswith(b"\xff\xd8"), "JPEG signature does not match MIME")
    offset, size, scans, components = 2, None, 0, set()
    sof = {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}
    while offset < len(body):
        _require(body[offset] == 0xff, "Invalid JPEG marker")
        while offset < len(body) and body[offset] == 0xff: offset += 1
        _require(offset < len(body), "Truncated JPEG marker")
        marker = body[offset]; offset += 1
        if marker == 0xd9:
            _require(size is not None and scans > 0, "JPEG ending lacks an image scan")
            trailer = body[offset:]
            if trailer:
                # Some Huawei JPEGs retain their camera ctrace block after EOI.
                # It has an explicit payload length and fixed 8-byte footer;
                # arbitrary trailing data, including another file, is rejected.
                _require(len(trailer) > 12 and trailer[-8:] == b"ctrace\0\0" and
                         int.from_bytes(trailer[-12:-8], "little") == len(trailer) - 12,
                         "Unrecognized JPEG trailing data")
            return size
        _require(marker == 0x01 or (0xc0 <= marker <= 0xfe and marker != 0xd8 and not 0xd0 <= marker <= 0xd7), "Invalid JPEG segment marker")
        if marker == 0x01: continue
        _require(offset + 2 <= len(body), "Truncated JPEG segment")
        length = int.from_bytes(body[offset:offset + 2], "big")
        _require(length >= 2 and offset + length <= len(body), "Invalid JPEG segment size")
        if marker in sof:
            _require(length >= 8 and size is None and body[offset + 7] > 0 and
                     length == 8 + 3 * body[offset + 7], "Invalid JPEG frame header")
            height, width = struct.unpack(">HH", body[offset + 3:offset + 7]); size = width, height
            ids = body[offset + 8:offset + length:3]
            components = set(ids)
            _require(len(components) == len(ids), "Duplicate JPEG frame component")
        if marker == 0xda:
            _require(size is not None and length >= 6 and body[offset + 2] > 0 and
                     length == 6 + 2 * body[offset + 2], "Invalid JPEG scan header")
            scan_ids = body[offset + 3:offset + length - 3:2]
            _require(len(set(scan_ids)) == len(scan_ids) and set(scan_ids).issubset(components), "Invalid JPEG scan components")
            offset += length
            start = offset
            while True:
                boundary = body.find(b"\xff", offset)
                _require(boundary >= 0 and boundary + 1 < len(body), "JPEG scan is truncated before EOI")
                following = boundary + 1
                while following < len(body) and body[following] == 0xff: following += 1
                _require(following < len(body), "Truncated JPEG scan marker")
                code = body[following]
                if code == 0x00 or 0xd0 <= code <= 0xd7:
                    offset = following + 1
                    continue
                _require(boundary > start, "JPEG scan data is missing")
                offset = boundary
                break
            scans += 1
            continue
        offset += length
    raise ValueError("JPEG EOI is missing")


def _webp(body):
    _require(len(body) >= 20 and body[:4] == b"RIFF" and body[8:12] == b"WEBP" and int.from_bytes(body[4:8], "little") + 8 == len(body), "WebP signature or RIFF size does not match")
    offset, canvas, frame, animation = 12, None, None, False
    while offset + 8 <= len(body):
        kind = body[offset:offset + 4]; length = int.from_bytes(body[offset + 4:offset + 8], "little")
        end = offset + 8 + length
        _require(end + (length & 1) <= len(body), "Truncated WebP chunk")
        chunk = body[offset + 8:end]
        if kind == b"VP8X":
            _require(length == 10 and canvas is None, "Invalid WebP canvas header")
            canvas = int.from_bytes(chunk[4:7], "little") + 1, int.from_bytes(chunk[7:10], "little") + 1
        elif kind == b"VP8 ":
            _require(length >= 10 and chunk[3:6] == b"\x9d\x01\x2a" and not chunk[0] & 1, "Invalid WebP VP8 frame")
            frame = int.from_bytes(chunk[6:8], "little") & 0x3fff, int.from_bytes(chunk[8:10], "little") & 0x3fff
        elif kind == b"VP8L":
            _require(length >= 5 and chunk[0] == 0x2f and chunk[4] >> 5 == 0, "Invalid WebP lossless frame")
            frame = 1 + chunk[1] + ((chunk[2] & 0x3f) << 8), 1 + (chunk[2] >> 6) + (chunk[3] << 2) + ((chunk[4] & 15) << 10)
        elif kind == b"ANMF":
            _require(length >= 16, "Invalid WebP animation frame")
            animation = True
        offset = end + (length & 1)
    _require(offset == len(body) and (frame is not None or (canvas is not None and animation)), "WebP has no image frame")
    if canvas and frame and not animation: _require(canvas == frame, "WebP frame and canvas disagree")
    return canvas or frame


def _gif(body):
    _require(len(body) >= 14 and body[:6] in (b"GIF87a", b"GIF89a"), "GIF signature does not match MIME")
    width, height = struct.unpack("<HH", body[6:10]); flags = body[10]
    offset = 13 + (3 * (1 << ((flags & 7) + 1)) if flags & 0x80 else 0)
    images = 0
    def blocks(at):
        while at < len(body):
            length = body[at]; at += 1
            if not length: return at
            _require(at + length <= len(body), "Truncated GIF data block")
            at += length
        raise ValueError("Truncated GIF data")
    while offset < len(body):
        marker = body[offset]; offset += 1
        if marker == 0x3b:
            _require(images > 0 and offset == len(body), "Invalid GIF ending")
            return width, height
        if marker == 0x21:
            _require(offset < len(body), "Truncated GIF extension")
            offset = blocks(offset + 1)
        elif marker == 0x2c:
            _require(offset + 9 < len(body), "Truncated GIF image descriptor")
            left, top, fw, fh, packed = struct.unpack("<HHHHB", body[offset:offset + 9]); offset += 9
            _require(fw > 0 and fh > 0 and left + fw <= width and top + fh <= height, "GIF frame exceeds canvas")
            if packed & 0x80: offset += 3 * (1 << ((packed & 7) + 1))
            _require(offset < len(body) and 2 <= body[offset] <= 8, "Invalid GIF LZW header")
            offset = blocks(offset + 1); images += 1
        else:
            raise ValueError("Invalid GIF block")
    raise ValueError("GIF trailer is missing")


def _dzi(body):
    root = _xml(body)
    _require(root.tag.rsplit("}", 1)[-1] == "Image" and len(root) == 1 and root[0].tag.rsplit("}", 1)[-1] == "Size", "Invalid DZI descriptor")
    _require(root.get("Format") in ("jpg", "jpeg", "png") and str(root.get("TileSize", "")).isdigit() and int(root.get("TileSize")) > 0 and root.get("Overlap", "0").isdigit(), "Invalid DZI tile settings")
    _require(set(root.attrib).issubset({"Format", "TileSize", "Overlap"}), "Unexpected DZI external settings")
    try:
        return int(root[0].get("Width")), int(root[0].get("Height"))
    except (TypeError, ValueError):
        raise ValueError("Invalid DZI dimensions") from None


def _image_type(record):
    path = record.get("archive_path", record.get("path"))
    _relative(path)
    suffix = PurePosixPath(path).suffix.lower()
    expected = IMAGE_MIMES.get(suffix)
    if suffix == ".dzi": expected = "application/xml"
    _require(expected is not None and record.get("mime") == expected, "Image suffix and MIME disagree")
    width, height = record.get("width"), record.get("height")
    _require((_positive(width) and _positive(height)) or (suffix == ".dzi" and width is None and height is None), "Invalid declared image dimensions")
    return suffix


class ImageStore:
    """One immutable, lazy scope/bundle snapshot per publisher validation run."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._scope = None
        self._bundles = None

    def _load_scope(self):
        if self._scope is None:
            scope = _json(_local(self.root, "registry/public-image-scope.json"))
            _require(isinstance(scope, dict) and type(scope.get("schema_version")) is int and scope["schema_version"] == 1 and scope.get("projection_required") is True, "Public image scope must remain enabled")
            for key in ("excluded_identity_sha256s", "excluded_content_sha256s"):
                values = scope.get(key)
                _require(isinstance(values, list) and all(_hash(x) for x in values) and len(set(values)) == len(values), "Invalid public image exclusions")
            self._scope = (set(scope["excluded_identity_sha256s"]), set(scope["excluded_content_sha256s"]))
        return self._scope

    def validate_identity(self, item):
        identities, unused = self._load_scope()
        _require(isinstance(item, dict), "Invalid image identity record")
        values = []
        for key in ("id", "original_id"):
            if key in item:
                _require(isinstance(item[key], str) and item[key], "Invalid image identity")
                values.append(item[key])
        for key in ("original_ids", "aliases", "image_ids"):
            if key in item:
                _require(isinstance(item[key], list) and all(isinstance(x, str) and x for x in item[key]), "Invalid image identity aliases")
                values.extend(item[key])
        _require(not any(hashlib.sha256(x.encode()).hexdigest() in identities for x in values), "Excluded image identity cannot be published")

    def _load_bundles(self):
        if self._bundles is not None: return self._bundles
        unused, excluded = self._load_scope()
        data = _json(_local(self.root, "registry/public-image-bundles.json"))
        _require(isinstance(data, dict) and type(data.get("schema_version")) is int and data["schema_version"] == 1 and isinstance(data.get("items"), list), "Invalid image bundle registry")
        bundles, assets, urls = {}, set(), set()
        for bundle in data["items"]:
            _version(bundle)
            identity, name = bundle.get("id"), bundle.get("name")
            _require(isinstance(identity, str) and SLUG.fullmatch(identity) and identity not in bundles, "Duplicate or invalid bundle ID")
            _relative(identity)
            _require(isinstance(name, str) and SLUG.fullmatch(name) and name.lower().endswith(".zip"), "Invalid bundle name")
            _require(isinstance(bundle.get("download_url"), str), "Bundle URL must be a string")
            parsed = urlsplit(bundle.get("download_url", ""))
            pieces = parsed.path.split("/")
            _require(parsed.scheme == "https" and parsed.netloc == "github.com" and not parsed.query and not parsed.fragment and len(pieces) == 7 and pieces[1:5] == ["hehefa", "-p4-theater-archive", "releases", "download"] and SLUG.fullmatch(unquote(pieces[5])) and unquote(pieces[6]) == name, "Bundle URL must name the exact GitHub release asset")
            _require(bundle["download_url"] not in urls and _positive(bundle.get("asset_id")) and bundle["asset_id"] not in assets and _positive(bundle.get("release_id")), "Duplicate or invalid GitHub asset identity")
            _require(bundle.get("asset_state") == "uploaded" and bundle.get("release_state") == "published", "Release must be published and asset uploaded")
            _require(bundle.get("download_readback_sha256") == bundle["sha256"] and type(bundle.get("download_readback_bytes")) is int and bundle["download_readback_bytes"] == bundle["bytes"], "Release download readback differs from registered bundle")
            digest = bundle.get("github_digest")
            if digest not in (None, ""):
                _require(isinstance(digest, str) and digest == "sha256:" + bundle["sha256"], "GitHub bundle digest differs")
            _timestamp(bundle.get("observed_at"))
            members, folded = {}, set()
            _require(isinstance(bundle.get("members"), list) and bundle["members"], "Bundle member inventory is required")
            for member in bundle["members"]:
                _version(member); _relative(member.get("path")); _no_link(member)
                key = unicodedata.normalize("NFC", member["path"]).casefold()
                _require(key not in folded, "Duplicate bundle member")
                _require(member["sha256"] not in excluded, "Bundle contains excluded image content")
                _require(isinstance(member.get("mime"), str) and member["mime"] and not any(ord(c) < 32 for c in member["mime"]), "Invalid bundle member MIME")
                _require(isinstance(member.get("role"), str) and member["role"], "Invalid bundle member role")
                _require((member.get("width") is None and member.get("height") is None) or (_positive(member.get("width")) and _positive(member.get("height"))), "Invalid bundle member dimensions")
                self.validate_identity(member)
                members[member["path"]] = member; folded.add(key)
            bundles[identity] = (bundle, members); assets.add(bundle["asset_id"]); urls.add(bundle["download_url"])
        self._bundles = bundles
        return bundles

    def validate_file(self, record):
        _version(record); self.validate_identity(record)
        _require(record["sha256"] not in self._load_scope()[1], "Excluded image content cannot be published")
        _no_link(record)
        suffix = _image_type(record)
        # An existing registry is one approved package set, including members
        # that are not the particular file currently being rendered.
        registry = self.root / "registry/public-image-bundles.json"
        if registry.exists() or registry.is_symlink(): self._load_bundles()
        storage = record.get("storage", "git")
        if storage == "release":
            identity = record.get("bundle_id")
            _require(isinstance(identity, str) and identity in self._load_bundles(), "Image bundle is not registered")
            bundle, members = self._bundles[identity]
            archive_path = record.get("archive_path"); _relative(archive_path)
            _require(record.get("path") == "published/packaged-images/" + identity + "/" + archive_path, "Virtual image path must exactly bind the bundle member")
            member = members.get(archive_path)
            _require(member is not None, "Image archive member is not registered")
            _require(record.get("download_url") == bundle["download_url"] and type(record.get("asset_id")) is int and record["asset_id"] == bundle["asset_id"], "Image release receipt identity differs")
            if "release_id" in record: _require(type(record["release_id"]) is int and record["release_id"] == bundle["release_id"], "Image release ID differs")
            for key in ("sha256", "bytes", "mime", "width", "height"):
                _require(key in record and record[key] == member.get(key), "Image and bundle member metadata differ: " + key)
            if "role" in record: _require(record["role"] == member.get("role"), "Image role differs from bundle member")
            return None
        _require(storage == "git" and isinstance(record.get("path"), str) and record["path"].startswith("published/images/"), "Git image requires published/images path")
        body = _local(self.root, record["path"]).read_bytes(); _checked_body(record, body)
        decoders = {".png": _png, ".jpg": _jpeg, ".jpeg": _jpeg, ".webp": _webp, ".gif": _gif, ".svg": _svg, ".dzi": _dzi}
        width, height = decoders[suffix](body)
        _require(_positive(width) and _positive(height), "Invalid actual image dimensions")
        if record.get("width") is not None:
            _require((width, height) == (record["width"], record["height"]), "Image dimensions differ from recorded dimensions")
        return body


def validate_document_bytes(record, body):
    """Check existing PDF/DOCX access copies, without executing or extracting them."""
    _checked_body(record, body)
    parts = _relative(record.get("path")); suffix = PurePosixPath(record["path"]).suffix.lower()
    _require(parts[:2] == ["published", "materials"], "Public binary document requires published/materials path")
    expected = {".pdf": "application/pdf", ".docx": DOCX_MIME}.get(suffix)
    _require(expected is not None and record.get("mime", expected) == expected, "Document suffix and MIME disagree")
    if suffix == ".pdf":
        _require(re.match(rb"%PDF-\d\.\d(?:\s|$)", body) is not None and body.rstrip().endswith(b"%%EOF"), "PDF signature or ending is invalid")
        return
    _require(body.startswith(b"PK\x03\x04"), "DOCX must be an ordinary ZIP file")
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names, folded, total = set(), set(), 0
            for member in archive.infolist():
                path = member.filename.rstrip("/") if member.is_dir() else member.filename
                _relative(path)
                key = unicodedata.normalize("NFC", path).casefold()
                _require(key not in folded and not member.flag_bits & 1, "Duplicate or encrypted DOCX member")
                mode = member.external_attr >> 16
                _require(stat.S_IFMT(mode) in (0, stat.S_IFDIR if member.is_dir() else stat.S_IFREG), "DOCX link member is not allowed")
                _require(not re.search(r"(?:^|/)(?:vbaproject\.bin|vbadata\.xml)$", path, re.I), "VBA is not allowed in DOCX")
                total += member.file_size; names.add(path); folded.add(key)
            _require(total <= 128 * 1024 * 1024, "DOCX exceeds bounded validation size")
            _require({"[Content_Types].xml", "word/document.xml"}.issubset(names), "DOCX required XML is missing")
            types_body, document_body = archive.read("[Content_Types].xml"), archive.read("word/document.xml")
            _require(b"macroenabled" not in types_body.lower() and b"vbaproject" not in types_body.lower(), "Macro-enabled DOCX is not allowed")
            types = _xml(types_body); document = _xml(document_body)
            document_roots = {"{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document",
                              "{http://purl.oclc.org/ooxml/wordprocessingml/main}document"}
            _require(types.tag.rsplit("}", 1)[-1] == "Types" and document.tag in document_roots, "Invalid DOCX XML roots")
            _require(any(x.get("PartName") == "/word/document.xml" and x.get("ContentType") == DOCX_MIME + ".main+xml" for x in types), "DOCX main content type is missing")
            _require(archive.testzip() is None, "DOCX ZIP CRC mismatch")
    except (zipfile.BadZipFile, KeyError, RuntimeError, NotImplementedError, EOFError):
        raise ValueError("Invalid DOCX ZIP") from None
