"""Shared helpers for Phase 1.5 attachment processing.

Image downscaling, MD5 hashing of binary resources, and rewriting of inline
<en-media> references within ENEX <content> CDATA.
"""
from __future__ import annotations

import hashlib
import html
import io
import re

from lxml import etree
from PIL import Image
import pillow_heif

pillow_heif.register_heif_opener()

# Notion hard cap is 5 MB; the 4.5 MB target leaves a safety margin.
TARGET_BYTES = int(4.5 * 1024 * 1024)
DOWNSCALE_MAX_EDGE = 2048
QUALITY_LADDER = [85, 75, 65, 55]
MIN_EDGE = 800  # never shrink below this dimension

# Aspect ratio above this flags an image as a likely screenshot worth visual review.
PRIORITY_ASPECT_RATIO = 2.0


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def resource_mime(resource: etree._Element) -> str:
    el = resource.find('mime')
    return el.text if el is not None and el.text else ''


def resource_data_bytes(resource: etree._Element) -> bytes:
    """Return decoded binary content of <resource><data>."""
    el = resource.find('data')
    if el is None or not el.text:
        return b''
    import base64
    # ENEX data is base64 with whitespace; clean it before decoding.
    cleaned = re.sub(r'\s+', '', el.text)
    return base64.b64decode(cleaned)


def resource_filename(resource: etree._Element, fallback_hash: str = '') -> str:
    attrs = resource.find('resource-attributes')
    if attrs is not None:
        fn = attrs.find('file-name')
        if fn is not None and fn.text:
            return fn.text
    # Best-effort fallback from MIME
    mime = resource_mime(resource)
    ext_map = {
        'image/jpeg': 'jpg',
        'image/png': 'png',
        'image/heic': 'heic',
        'image/gif': 'gif',
        'application/pdf': 'pdf',
        'audio/x-m4a': 'm4a',
        'audio/mpeg': 'mp3',
        'video/mp4': 'mp4',
    }
    ext = ext_map.get(mime, 'bin')
    stem = fallback_hash[:12] if fallback_hash else 'attachment'
    return f'{stem}.{ext}'


def set_resource_data(resource: etree._Element, new_binary: bytes, new_mime: str) -> None:
    """Replace <data> content (base64) and <mime> on a <resource>."""
    import base64
    new_b64 = base64.b64encode(new_binary).decode('ascii')
    # Re-flow to 76-char lines like the original (cosmetic, matches evernote-backup style)
    flowed = '\n'.join(new_b64[i:i + 76] for i in range(0, len(new_b64), 76))

    data_el = resource.find('data')
    if data_el is not None:
        data_el.text = '\n' + flowed + '\n'
        data_el.set('encoding', 'base64')

    mime_el = resource.find('mime')
    if mime_el is not None:
        mime_el.text = new_mime


def downscale_image(
    binary: bytes, source_mime: str
) -> tuple[bytes, str, tuple[int, int], tuple[int, int]]:
    """Downscale an image until its serialized size is under TARGET_BYTES.

    Returns (new_binary, new_mime, original_dims, new_dims).
    HEIC transcodes to JPEG. PNG-with-transparency stays PNG; all other PNGs
    convert to JPEG for better compression.
    """
    img = Image.open(io.BytesIO(binary))
    img.load()
    original_dims = img.size

    has_alpha = img.mode in ('RGBA', 'LA') or (
        img.mode == 'P' and 'transparency' in img.info
    )

    if source_mime == 'image/heic' or (source_mime == 'image/png' and not has_alpha) or source_mime == 'image/jpeg':
        out_format = 'JPEG'
        out_mime = 'image/jpeg'
        if img.mode != 'RGB':
            img = img.convert('RGB')
    elif source_mime == 'image/png' and has_alpha:
        out_format = 'PNG'
        out_mime = 'image/png'
    else:
        out_format = 'JPEG'
        out_mime = 'image/jpeg'
        if img.mode != 'RGB':
            img = img.convert('RGB')

    # Initial cap on largest edge.
    if max(img.size) > DOWNSCALE_MAX_EDGE:
        scale = DOWNSCALE_MAX_EDGE / max(img.size)
        new_w = max(int(img.size[0] * scale), 1)
        new_h = max(int(img.size[1] * scale), 1)
        img = img.resize((new_w, new_h), Image.LANCZOS)

    def encode(image: Image.Image, quality: int) -> bytes:
        buf = io.BytesIO()
        kwargs = {'format': out_format, 'optimize': True}
        if out_format == 'JPEG':
            kwargs['quality'] = quality
            kwargs['progressive'] = True
        image.save(buf, **kwargs)
        return buf.getvalue()

    # Try quality ladder at current dimensions.
    encoded = b''
    for q in QUALITY_LADDER:
        encoded = encode(img, q)
        if len(encoded) <= TARGET_BYTES:
            return encoded, out_mime, original_dims, img.size

    # Still too big — iteratively shrink at quality 65.
    while max(img.size) > MIN_EDGE:
        new_edge = max(int(max(img.size) * 0.8), MIN_EDGE)
        scale = new_edge / max(img.size)
        img = img.resize(
            (max(int(img.size[0] * scale), 1), max(int(img.size[1] * scale), 1)),
            Image.LANCZOS,
        )
        encoded = encode(img, 65)
        if len(encoded) <= TARGET_BYTES:
            return encoded, out_mime, original_dims, img.size

    # Gave it our best shot — return whatever we ended with.
    return encoded, out_mime, original_dims, img.size


def is_priority_review(
    source_mime: str, original_dims: tuple[int, int]
) -> bool:
    """Flag images that warrant visual confirmation after downscale."""
    if source_mime == 'image/png':
        return True
    w, h = original_dims
    if min(w, h) <= 0:
        return False
    return max(w, h) / min(w, h) > PRIORITY_ASPECT_RATIO


def update_enmedia_hash(content: str, old_hash: str, new_hash: str, new_mime: str) -> str:
    """Update hash (and type) attributes on any <en-media> referencing old_hash."""
    if not old_hash or not content:
        return content

    def replace(m: re.Match) -> str:
        tag = m.group(0)
        tag = re.sub(r'hash="[^"]*"', f'hash="{new_hash}"', tag, count=1)
        if re.search(r'type="', tag):
            tag = re.sub(r'type="[^"]*"', f'type="{new_mime}"', tag, count=1)
        else:
            tag = tag.replace('<en-media', f'<en-media type="{new_mime}"', 1)
        return tag

    pattern = re.compile(rf'<en-media\b[^>]*\bhash="{re.escape(old_hash)}"[^>]*/?>')
    return pattern.sub(replace, content)


def replace_enmedia_with_link(
    content: str, old_hash: str, url: str, label: str
) -> str:
    """Replace any <en-media hash="old_hash"...> with an HTML anchor."""
    if not old_hash or not content:
        return content
    anchor = f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'
    pattern = re.compile(rf'<en-media\b[^>]*\bhash="{re.escape(old_hash)}"[^>]*/?>')
    return pattern.sub(anchor, content)


def replace_enmedia_with_marker(content: str, old_hash: str, label: str) -> str:
    """Replace <en-media> with a plain-text marker (used when user skips an offload)."""
    if not old_hash or not content:
        return content
    marker = html.escape(f'[Attachment removed: {label}]')
    pattern = re.compile(rf'<en-media\b[^>]*\bhash="{re.escape(old_hash)}"[^>]*/?>')
    return pattern.sub(marker, content)
