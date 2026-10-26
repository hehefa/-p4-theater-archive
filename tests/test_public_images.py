import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from public_images import ImageStore, validate_document_bytes


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, ensure_ascii=False) + "\n").encode())


def digest(body):
    return hashlib.sha256(body).hexdigest()


def png(width=2, height=3):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress((b"\0" + b"\0" * (3 * width)) * height)) + chunk(b"IEND", b"")


def jpeg(width=2, height=3):
    return b"\xff\xd8\xff\xc0\x00\x0b\x08" + struct.pack(">HH", height, width) + b"\x01\x01\x11\x00\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x00\xff\xd9"


def webp(width=2, height=3):
    bits = (width - 1) | ((height - 1) << 14)
    data = b"\x2f" + bits.to_bytes(4, "little")
    chunk = b"VP8L" + len(data).to_bytes(4, "little") + data + b"\0"
    return b"RIFF" + (4 + len(chunk)).to_bytes(4, "little") + b"WEBP" + chunk


def file_record(path, body, mime, width=2, height=3):
    return {"path": path, "sha256": digest(body), "bytes": len(body), "mime": mime,
            "width": width, "height": height, "role": "canonical", "image_ids": ["hf-img-fixture"]}


class PublicImageTests(unittest.TestCase):
    def fixture(self, root):
        write_json(root / "registry/public-image-scope.json", {"schema_version": 1, "projection_required": True,
                   "excluded_identity_sha256s": [], "excluded_content_sha256s": []})

    def git_file(self, root, suffix=".png", body=None, mime="image/png", width=2, height=3):
        if body is None: body = png(width, height)
        path = root / "published/images" / ("fixture" + suffix)
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(body)
        return file_record(path.relative_to(root).as_posix(), body, mime, width, height)

    def bundle(self, root):
        body = png()
        member = file_record("media/fixture.png", body, "image/png")
        bundle = {"id": "fixture-core", "name": "fixture-core.zip", "bytes": 1000, "sha256": "a" * 64,
                  "download_url": "https://github.com/hehefa/-p4-theater-archive/releases/download/official-v2/fixture-core.zip",
                  "asset_id": 123, "release_id": 456, "asset_state": "uploaded", "release_state": "published",
                  "github_digest": "sha256:" + "a" * 64, "download_readback_sha256": "a" * 64,
                  "download_readback_bytes": 1000, "observed_at": "2026-10-26T00:00:00+00:00", "members": [member]}
        write_json(root / "registry/public-image-bundles.json", {"schema_version": 1, "items": [bundle]})
        record = dict(member, storage="release", bundle_id=bundle["id"], archive_path=member["path"],
                      path="published/packaged-images/fixture-core/" + member["path"],
                      download_url=bundle["download_url"], asset_id=123)
        return bundle, record

    def test_git_checks_bytes_dimensions_and_all_common_image_signatures(self):
        gif = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="3"><text>Literal javascript: is text</text></svg>'
        cases = [(".png", png(), "image/png", 2, 3), (".jpg", jpeg(), "image/jpeg", 2, 3),
                 (".webp", webp(), "image/webp", 2, 3), (".gif", gif, "image/gif", 1, 1),
                 (".svg", svg, "image/svg+xml", 2, 3)]
        for suffix, body, mime, width, height in cases:
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); self.fixture(root)
                record = self.git_file(root, suffix, body, mime, width, height)
                self.assertEqual(ImageStore(root).validate_file(record), body)
                record["width"] += 1
                with self.assertRaisesRegex(ValueError, "dimensions"): ImageStore(root).validate_file(record)

    def test_scope_and_bundle_are_lazy_but_scope_is_required_for_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); store = ImageStore(root)
            record = self.git_file(root)
            with self.assertRaises(ValueError): store.validate_file(record)
            self.fixture(root)
            self.assertEqual(ImageStore(root).validate_file(record), png())
            write_json(root / "registry/public-image-bundles.json", {"intentionally": "unneeded for Git files"})
            with self.assertRaises(ValueError): ImageStore(root).validate_file(record)

    def test_jpeg_requires_complete_scan_and_only_recognized_length_bound_trailer(self):
        image = jpeg()
        payload = b"\0" * 168 + b"camera trace fixture"
        trailer = payload + struct.pack("<I", len(payload)) + b"ctrace\0\0"
        scan = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
        # Stuffed FF and restart markers occur inside the entropy-coded scan.
        stuffed = image.replace(scan + b"\x00", scan + b"\x00\xff\x00\x01\xff\xd0\x02")
        # Progressive images can contain multiple separate scans.
        multiple = image[:-2] + scan + b"\x00\xff\xd9"
        for body in (image, image + trailer, stuffed, multiple):
            with self.subTest(valid=digest(body)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); self.fixture(root)
                record = self.git_file(root, ".jpg", body, "image/jpeg")
                self.assertEqual(ImageStore(root).validate_file(record), body)
        invalid = [image[:-2], image[:-1], image + b"arbitrary trailing data",
                   image + trailer[:-1], image + payload + struct.pack("<I", len(payload) + 1) + b"ctrace\0\0",
                   image.replace(scan + b"\x00", scan), image[:-2] + b"\xff\xe1\x00\x20\x00\xff\xd9",
                   image.replace(scan + b"\x00", scan + b"\xff\xd8"),
                   image.replace(scan, b"\xff\xda\x00\x08\x01\x09\x00\x00\x3f\x00"),
                   image.replace(b"\xff\xc0\x00\x0b", b"\xff\xc0\x00\x0a")]
        for body in invalid:
            with self.subTest(invalid=digest(body)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); self.fixture(root)
                record = self.git_file(root, ".jpg", body, "image/jpeg")
                with self.assertRaises(ValueError): ImageStore(root).validate_file(record)

    def test_tampered_file_mime_magic_size_and_traversal_fail(self):
        for mutation in ("bytes", "sha256", "mime", "suffix", "path", "signature", "dimensions"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); self.fixture(root); record = self.git_file(root)
                if mutation == "bytes": record["bytes"] += 1
                elif mutation == "sha256": record["sha256"] = "b" * 64
                elif mutation == "mime": record["mime"] = "image/jpeg"
                elif mutation == "suffix": record["path"] = record["path"].replace(".png", ".txt")
                elif mutation == "path": record["path"] = "published/images/../fixture.png"
                elif mutation == "dimensions": record["height"] = True
                else:
                    body = b"not an image"; (root / record["path"]).write_bytes(body)
                    record.update(sha256=digest(body), bytes=len(body))
                with self.assertRaises(ValueError): ImageStore(root).validate_file(record)

    def test_local_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.fixture(root); record = self.git_file(root)
            source = root / record["path"]; target = root / "outside.png"; source.rename(target)
            try: source.symlink_to(target)
            except OSError: self.skipTest("Symlink creation is unavailable")
            with self.assertRaisesRegex(ValueError, "symlink"): ImageStore(root).validate_file(record)

    def test_active_svg_dtd_and_external_references_are_rejected(self):
        payloads = [b'<!DOCTYPE svg [<!ENTITY x "x">]><svg width="2" height="3"/>',
                    b'<svg width="2" height="3" onload="alert(1)"/>',
                    b'<svg width="2" height="3"><script>alert(1)</script></svg>',
                    b'<svg width="2" height="3"><image href="https://example.org/x.svg"/></svg>',
                    b'<svg width="2" height="3"><foreignObject/></svg>',
                    b'<svg width="2" height="3"><rect fill="url(https://example.org/a)"/></svg>']
        for body in payloads:
            with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); self.fixture(root)
                record = self.git_file(root, ".svg", body, "image/svg+xml")
                with self.assertRaises(ValueError): ImageStore(root).validate_file(record)

    def test_release_returns_none_without_virtual_local_file_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.fixture(root); unused, record = self.bundle(root)
            self.assertFalse((root / record["path"]).exists())
            self.assertIsNone(ImageStore(root).validate_file(record))

    def test_fake_receipt_states_hashes_ids_hosts_and_duplicate_bundles_fail(self):
        mutations = {"sha": lambda b: b.update(download_readback_sha256="b" * 64),
                     "size": lambda b: b.update(download_readback_bytes=999),
                     "digest": lambda b: b.update(github_digest="sha256:" + "b" * 64),
                     "draft": lambda b: b.update(release_state="draft"),
                     "unuploaded": lambda b: b.update(asset_state="new"),
                     "asset_bool": lambda b: b.update(asset_id=True),
                     "time": lambda b: b.update(observed_at="2026-10-26"),
                     "host": lambda b: b.update(download_url=b["download_url"].replace("github.com", "github.com.evil.test")),
                     "owner": lambda b: b.update(download_url=b["download_url"].replace("hehefa/", "other/")),
                     "query": lambda b: b.update(download_url=b["download_url"] + "?download=1"),
                     "duplicate_id": lambda b: None}
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); self.fixture(root); bundle, record = self.bundle(root); mutate(bundle)
                items = [bundle, deepcopy(bundle)] if name == "duplicate_id" else [bundle]
                write_json(root / "registry/public-image-bundles.json", {"schema_version": 1, "items": items})
                with self.assertRaises(ValueError): ImageStore(root).validate_file(record)

    def test_unsafe_duplicate_link_members_and_member_metadata_fail(self):
        for mutation in ("absolute", "traversal", "backslash", "drive", "percent", "duplicate", "case_alias", "symlink", "mode", "record_hash", "record_size", "record_asset", "record_url", "record_path", "record_mime", "record_dims"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); self.fixture(root); bundle, record = self.bundle(root); member = bundle["members"][0]
                if mutation in ("absolute", "traversal", "backslash", "drive", "percent"):
                    member["path"] = {"absolute": "/outside.png", "traversal": "../outside.png", "backslash": "a\\x.png", "drive": "C:/x.png", "percent": "a/%2e%2e/x.png"}[mutation]
                elif mutation in ("duplicate", "case_alias"):
                    clone = deepcopy(member)
                    if mutation == "case_alias": clone["path"] = clone["path"].upper()
                    bundle["members"].append(clone)
                elif mutation == "symlink": member["is_symlink"] = True
                elif mutation == "mode": member["external_attr"] = 0o120777 << 16
                else:
                    key = {"record_hash": "sha256", "record_size": "bytes", "record_asset": "asset_id", "record_url": "download_url", "record_path": "path", "record_mime": "mime", "record_dims": "width"}[mutation]
                    record[key] = {"sha256": "b" * 64, "bytes": 1, "asset_id": 987, "download_url": bundle["download_url"] + "/x", "path": "published/packaged-images/wrong/media/fixture.png", "mime": "image/gif", "width": 77}[key]
                write_json(root / "registry/public-image-bundles.json", {"schema_version": 1, "items": [bundle]})
                with self.assertRaises(ValueError): ImageStore(root).validate_file(record)

    def test_hold_scope_blocks_identity_file_and_unreferenced_bundle_member(self):
        for mutation in ("identity", "alias", "git_bytes", "bundle_bytes", "bundle_bytes_on_git"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); self.fixture(root); bundle, release = self.bundle(root); record = self.git_file(root)
                scope = {"schema_version": 1, "projection_required": True, "excluded_identity_sha256s": [], "excluded_content_sha256s": []}
                if mutation in ("identity", "alias"):
                    scope["excluded_identity_sha256s"] = [digest(b"hf-img-held")]
                else:
                    scope["excluded_content_sha256s"] = [record["sha256"] if mutation == "git_bytes" else "b" * 64]
                if mutation in ("bundle_bytes", "bundle_bytes_on_git"):
                    bundle["members"].append(dict(bundle["members"][0], path="media/held.png", sha256="b" * 64))
                    write_json(root / "registry/public-image-bundles.json", {"schema_version": 1, "items": [bundle]})
                write_json(root / "registry/public-image-scope.json", scope)
                store = ImageStore(root)
                with self.assertRaises(ValueError):
                    if mutation == "identity": store.validate_identity({"original_id": "hf-img-held"})
                    elif mutation == "alias": store.validate_identity({"id": "new-name", "aliases": ["hf-img-held"]})
                    else: store.validate_file(release if mutation == "bundle_bytes" else record)

    def test_dzi_auxiliary_is_exactly_scoped_and_can_have_null_dimensions(self):
        body = b'<Image xmlns="http://schemas.microsoft.com/deepzoom/2008" TileSize="254" Overlap="1" Format="jpg"><Size Width="2000" Height="3000"/></Image>'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.fixture(root)
            record = self.git_file(root, ".dzi", body, "application/xml", None, None)
            self.assertEqual(ImageStore(root).validate_file(record), body)
            bundle, release = self.bundle(root)
            member = file_record("media/image.dzi", body, "application/xml", None, None)
            member["role"] = "deep_zoom_descriptor"; bundle["members"] = [member]
            release.update(member, archive_path=member["path"], path="published/packaged-images/fixture-core/" + member["path"])
            write_json(root / "registry/public-image-bundles.json", {"schema_version": 1, "items": [bundle]})
            self.assertIsNone(ImageStore(root).validate_file(release))
            release["path"] = release["path"].replace(".dzi", ".xml")
            with self.assertRaises(ValueError): ImageStore(root).validate_file(release)

    def document(self, suffix, body):
        return {"path": "published/materials/test" + suffix, "sha256": digest(body), "bytes": len(body)}

    def docx(self, extras=None):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>')
            for name, body in extras or []: archive.writestr(name, body)
        return stream.getvalue()

    def test_pdf_and_docx_checks_require_real_container_types_without_mime_field(self):
        for suffix, body in [(".pdf", b"%PDF-1.7\nfixture\n%%EOF\n"), (".docx", self.docx())]:
            with self.subTest(suffix=suffix):
                self.assertIsNone(validate_document_bytes(self.document(suffix, body), body))
                record = self.document(suffix, body); record["mime"] = "text/html"
                with self.assertRaises(ValueError): validate_document_bytes(record, body)
        for suffix, body in [(".pdf", b"<html>not PDF</html>"), (".docx", b"not a zip"), (".docx", self.docx([("../escape", b"x")])), (".docx", self.docx([("word/vbaProject.bin", b"macro")])), (".docx", self.docx([("word/DOCUMENT.XML", b"duplicate")]))]:
            with self.subTest(suffix=suffix, sample=digest(body)):
                with self.assertRaises(ValueError): validate_document_bytes(self.document(suffix, body), body)


if __name__ == "__main__":
    unittest.main()
