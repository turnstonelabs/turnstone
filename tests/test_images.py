"""Tests for turnstone.core.images: EXIF orientation, and transparency flattened on the way in."""

from __future__ import annotations

import ast
import struct
import zlib
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from tests._exif_helpers import (
    exif_block,
    halves,
    jpeg_with_exif,
    orientation_entry,
    png_with_exif,
)
from tests._helpers import keyed_png
from turnstone.core import images
from turnstone.core.attachments import IMAGE_SIZE_CAP
from turnstone.core.images import (
    exif_orientation,
    flatten_image,
    needs_flattening,
    normalize_image_orientation,
)

Image = pytest.importorskip("PIL.Image")

_ORIENTATION_TAG = 0x0112  # EXIF orientation (standard tag id)


@pytest.fixture(autouse=True)
def _nothing_kept() -> None:
    images._normalized.clear()


def _oriented_jpeg(orientation: int, size: tuple[int, int] = (4, 2)) -> bytes:
    img = Image.new("RGB", size, "red")
    exif = img.getexif()
    exif[_ORIENTATION_TAG] = orientation
    buf = BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def test_applies_rotation_and_strips_tag() -> None:
    # Orientation 6 = "rotate 90° for display": a 4×2 landscape becomes 2×4.
    data = _oriented_jpeg(6, size=(4, 2))
    out = normalize_image_orientation(data)
    assert out != data, "a rotated image must be re-encoded upright"
    img = Image.open(BytesIO(out))
    assert img.size == (2, 4), "the 90° rotation must be baked into the pixels"
    assert img.getexif().get(_ORIENTATION_TAG) in (None, 1), "the orientation tag must be cleared"


def test_passthrough_when_upright() -> None:
    data = _oriented_jpeg(1, size=(4, 2))
    assert normalize_image_orientation(data) == data, "identity orientation must not re-encode"


def test_passthrough_when_no_exif() -> None:
    buf = BytesIO()
    Image.new("RGB", (3, 3), "blue").save(buf, format="PNG")
    data = buf.getvalue()
    assert normalize_image_orientation(data) == data, "a tag-less image must pass through verbatim"


def test_never_raises_on_garbage() -> None:
    assert normalize_image_orientation(b"not an image") == b"not an image"
    assert normalize_image_orientation(b"") == b""


def test_scan_limit() -> None:
    from tests._jpeg_helpers import jpeg_with_scans
    from turnstone.core.images import MAX_JPEG_SCANS, too_many_jpeg_scans

    own = jpeg_with_scans(0).count(b"\xff\xda")
    assert not too_many_jpeg_scans(jpeg_with_scans(MAX_JPEG_SCANS - own))
    assert too_many_jpeg_scans(jpeg_with_scans(MAX_JPEG_SCANS - own + 1))
    buf = BytesIO()
    Image.new("RGB", (3, 3), "blue").save(buf, format="PNG")
    assert not too_many_jpeg_scans(buf.getvalue() + b"\xff\xda" * (MAX_JPEG_SCANS + 1))


def test_a_jpeg_with_too_many_scans_is_passed_through_undecoded() -> None:
    # Each scan costs a decode pass over the whole picture.
    from tests._jpeg_helpers import jpeg_with_scans

    turned = jpeg_with_scans(0, orientation=6)
    assert normalize_image_orientation(turned) != turned
    slow = jpeg_with_scans(150, orientation=6)
    assert normalize_image_orientation(slow) == slow


def _webp(exif: bytes) -> bytes:
    buf = BytesIO()
    halves().save(buf, format="WEBP", exif=exif)
    return buf.getvalue()


def _png_with_text(key: str, text: str) -> bytes:
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_itxt(key, text)
    buf = BytesIO()
    halves().save(buf, format="PNG", pnginfo=info)
    return buf.getvalue()


def _raw_profile(exif: bytes) -> str:
    # The text form some tools write: a name, the length, then hex lines.
    hexed = exif.hex()
    lines = [hexed[i : i + 72] for i in range(0, len(hexed), 72)]
    return "\nexif\n" + f"{len(exif):8d}\n" + "\n".join(lines)


@pytest.mark.parametrize(
    ("source", "data", "expected"),
    [
        ("jpeg", lambda: jpeg_with_exif(exif_block([orientation_entry(6)])), 6),
        (
            "big-endian jpeg",
            lambda: jpeg_with_exif(exif_block([orientation_entry(3, ">")], ">")),
            3,
        ),
        ("png", lambda: png_with_exif(exif_block([orientation_entry(8)])), 8),
        ("webp", lambda: _webp(exif_block([orientation_entry(2)])), 2),
        ("none", lambda: png_with_exif(b""), 1),
    ],
)
def test_orientation_is_read_where_browsers_read_it(source: str, data: Any, expected: int) -> None:
    img = Image.open(BytesIO(data()))
    assert exif_orientation(img) == expected, "read at open, before any decode"
    assert img.getexif().get(_ORIENTATION_TAG, 1) == expected, "Pillow reads the same"


@pytest.mark.parametrize(
    ("source", "data"),
    [
        (
            "png, after its pixels",
            lambda: png_with_exif(exif_block([orientation_entry(5)]), after_pixels=True),
        ),
        (
            "png text",
            lambda: _png_with_text(
                "Raw profile type exif", _raw_profile(exif_block([orientation_entry(4)]))
            ),
        ),
        (
            "xmp",
            lambda: _png_with_text(
                "XML:com.adobe.xmp",
                '<x:xmpmeta><rdf:Description tiff:Orientation="6"/></x:xmpmeta>',
            ),
        ),
    ],
)
def test_orientation_stored_elsewhere_is_ignored(source: str, data: Any) -> None:
    # Browsers don't turn these, so neither do the thumbnail and the model's copy.
    picture = data()
    assert exif_orientation(Image.open(BytesIO(picture))) == 1
    assert normalize_image_orientation(picture) is picture


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ((0x0112, 3, 1, struct.pack("<HH", 6, 0)), 6),  # SHORT, as the standard says
        ((0x0112, 4, 1, struct.pack("<L", 6)), 6),  # LONG
        ((0x0112, 8, 1, struct.pack("<hh", 6, 0)), 6),  # SSHORT
        ((0x0112, 9, 1, struct.pack("<l", 6)), 6),  # SLONG
        ((0x0112, 1, 1, b"\x06\x00\x00\x00"), 1),  # BYTE: ignored
        ((0x0112, 3, 2, struct.pack("<HH", 6, 6)), 1),  # more than one value: ignored
    ],
)
def test_orientation_entry_types(entry: tuple[int, int, int, bytes], expected: int) -> None:
    img = Image.open(BytesIO(jpeg_with_exif(exif_block([entry]))))
    assert exif_orientation(img) == expected


@pytest.mark.parametrize(
    "exif",
    [
        b"II*\x00",  # stops after the byte order
        b"II*\x00" + struct.pack("<L", 4000) + bytes(8),  # directory past the end
    ],
)
@pytest.mark.parametrize("container", [jpeg_with_exif, png_with_exif])
@pytest.mark.filterwarnings("ignore:Corrupt EXIF data")
def test_malformed_exif_reads_as_upright(exif: bytes, container: Any) -> None:
    # A photo whose EXIF stops short is still sent, thumbnailed and accepted.
    from turnstone.core.avatars import normalize_picture
    from turnstone.core.thumbnails import make_thumbnail

    picture = container(exif)
    assert exif_orientation(Image.open(BytesIO(picture))) == 1
    assert normalize_image_orientation(picture) is picture
    assert make_thumbnail(picture, "image") is not None
    assert normalize_picture(picture)[0]


def test_a_rotated_photo_with_oddly_typed_tags_is_still_turned() -> None:
    # Pillow's own transpose rewrites the remaining EXIF, and fails on a tag
    # stored in a type other than the one it expects.
    resolution_unit = (0x0128, 4, 1, struct.pack("<L", 70000))
    data = jpeg_with_exif(exif_block([orientation_entry(6), resolution_unit]))
    assert Image.open(BytesIO(normalize_image_orientation(data))).size == (20, 40)


def test_a_rotated_photo_is_turned_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every send rebuilds the request from the stored originals.
    turns: list[int] = []
    real = images.turn_upright

    def counting(img: Any, orientation: int) -> Any:
        turns.append(orientation)
        return real(img, orientation)

    monkeypatch.setattr(images, "turn_upright", counting)
    data = _oriented_jpeg(6, size=(4, 2))
    first = normalize_image_orientation(data)
    assert normalize_image_orientation(data) is first
    assert turns == [6]


def test_an_upright_png_is_never_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    from PIL import PngImagePlugin

    loads: list[int] = []
    real = PngImagePlugin.PngImageFile.load

    def counting(self: Any) -> Any:
        loads.append(1)
        return real(self)

    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", counting)
    buf = BytesIO()
    Image.new("RGB", (3, 3), "blue").save(buf, format="PNG")
    data = buf.getvalue()
    assert normalize_image_orientation(data) is data
    assert loads == []


def test_kept_images_are_bounded() -> None:
    cache = images._NormalizedCache(max_bytes=10, max_entries=3)
    cache.put(b"a", b"x" * 6)
    cache.put(b"b", b"y")
    cache.put(b"c", b"x" * 6)  # 13 bytes: the least recently used goes
    assert cache.get(b"a") is None
    assert cache.get(b"b") == b"y"
    cache.put(b"d", b"z")
    cache.put(b"e", b"w")  # four entries: c goes, since b was just used
    assert cache.get(b"c") is None
    assert cache.get(b"b") == b"y"
    cache.put(b"f", b"x" * 11)  # more than the whole budget: not kept
    assert cache.get(b"f") is None


def _clear(size: tuple[int, int] = (60, 30), under: tuple[int, int, int] = (255, 255, 255)) -> Any:
    """A wholly transparent RGBA picture whose transparent pixels store ``under``."""
    return Image.new("RGBA", size, (*under, 0))


def _encoded(img: Any, fmt: str = "PNG", **params: Any) -> bytes:
    buf = BytesIO()
    img.save(buf, format=fmt, **params)
    return buf.getvalue()


_CAP = IMAGE_SIZE_CAP  # the upload and read_file image cap


def _sent(data: bytes) -> Any:
    """The picture stored, and every model sent, for an image entering a conversation."""
    return Image.open(BytesIO(flatten_image(data, max_bytes=_CAP)))


def test_light_content_is_flattened_onto_black() -> None:
    # #1445: white is stored under every pixel and the content is only in the alpha channel, so
    # filling with white or dropping the alpha, as providers do, leaves a blank white frame.
    img = _clear()
    img.paste((255, 255, 255, 255), (20, 10, 40, 20))
    sent = _sent(_encoded(img))
    assert (sent.format, sent.mode) == ("PNG", "RGB")
    assert sent.getpixel((0, 0)) == (0, 0, 0)
    assert sent.getpixel((30, 15)) == (255, 255, 255)


def test_dark_content_is_flattened_onto_white() -> None:
    img = _clear(under=(0, 0, 0))
    img.paste((0, 0, 0, 255), (20, 10, 40, 20))
    sent = _sent(_encoded(img))
    assert sent.getpixel((0, 0)) == (255, 255, 255)
    assert sent.getpixel((30, 15)) == (0, 0, 0)


def test_light_and_dark_content_go_onto_mid_grey() -> None:
    # Either extreme hides one of them; mid grey leaves both half the contrast.
    img = _clear()
    img.paste((255, 255, 255, 255), (5, 10, 25, 20))
    img.paste((0, 0, 0, 255), (35, 10, 55, 20))
    sent = _sent(_encoded(img))
    assert sent.getpixel((0, 0)) == (128, 128, 128)
    assert sent.getpixel((15, 15)) == (255, 255, 255)
    assert sent.getpixel((45, 15)) == (0, 0, 0)


def test_a_light_word_among_dark_ones_still_counts() -> None:
    # Mostly dark content would pick white by any average, and the one white word would vanish.
    img = _clear((200, 40))
    for x in range(0, 180, 20):
        img.paste((0, 0, 0, 255), (x + 2, 10, x + 14, 30))
    img.paste((255, 255, 255, 255), (182, 10, 194, 30))
    assert _sent(_encoded(img)).getpixel((0, 0)) == (128, 128, 128)


def test_a_stray_pixel_does_not_rule_out_a_background() -> None:
    img = _clear((200, 100), under=(0, 0, 0))
    img.paste((0, 0, 0, 255), (20, 20, 180, 80))
    img.putpixel((5, 5), (255, 255, 255, 255))
    assert _sent(_encoded(img)).getpixel((0, 0)) == (255, 255, 255)


def test_a_small_icon_of_one_light_colour_goes_onto_black() -> None:
    # Its 12 edge pixels are as few as a speck's, but they are all there is.
    img = _clear((16, 16))
    img.paste((255, 255, 255, 255), (6, 6, 10, 10))
    sent = _sent(_encoded(img))
    assert sent.getpixel((0, 0)) == (0, 0, 0)
    assert sent.getpixel((7, 7)) == (255, 255, 255)


@pytest.mark.parametrize(
    "colour",
    [(0, 255, 255), (0, 255, 0), (255, 255, 0), (255, 165, 0), (176, 176, 176)],
    ids=["cyan", "green", "yellow", "orange", "light grey"],
)
def test_bright_colours_go_onto_black(colour: tuple[int, int, int]) -> None:
    # Their luma sits well below white's, yet on white they barely show: cyan at 1.25:1.
    img = _clear(under=colour)
    img.paste((*colour, 255), (20, 10, 40, 20))
    assert _sent(_encoded(img)).getpixel((0, 0)) == (0, 0, 0)


@pytest.mark.parametrize(
    "colour", [(0, 0, 255), (255, 0, 0), (102, 102, 102)], ids=["blue", "red", "grey 666"]
)
def test_deep_colours_go_onto_white(colour: tuple[int, int, int]) -> None:
    # #666 is 5.7:1 on white and 3.7:1 on black, though its encoded value is 40% of white's.
    img = _clear(under=colour)
    img.paste((*colour, 255), (20, 10, 40, 20))
    assert _sent(_encoded(img)).getpixel((0, 0)) == (255, 255, 255)


def test_dark_text_with_bright_accents_goes_onto_mid_grey() -> None:
    img = _clear((120, 40))
    for x in range(5, 60, 12):
        img.paste((0, 0, 0, 255), (x, 10, x + 8, 30))
    for x in range(65, 110, 12):
        img.paste((0, 255, 255, 255), (x, 10, x + 8, 30))
    assert _sent(_encoded(img)).getpixel((0, 0)) == (128, 128, 128)


@pytest.mark.parametrize(
    ("text", "word", "expected"),
    [
        ((0, 0, 255), (255, 255, 255), (0, 0, 0)),
        ((68, 68, 68), (255, 255, 255), (0, 0, 0)),
        ((255, 165, 0), (0, 0, 0), (255, 255, 255)),
    ],
    ids=["blue text, white word", "#444 text, white word", "orange text, black word"],
)
def test_a_word_unlike_the_rest_stays_visible(
    text: tuple[int, int, int], word: tuple[int, int, int], expected: tuple[int, int, int]
) -> None:
    # The text blends with mid grey and with one extreme, so counting only edges that blend put
    # the word onto its own colour, where it vanished.  The text keeps 2:1 or so instead.
    img = _clear((200, 40))
    for x in range(0, 180, 20):
        img.paste((*text, 255), (x + 2, 10, x + 14, 30))
    img.paste((*word, 255), (182, 10, 194, 30))
    assert _sent(_encoded(img)).getpixel((0, 0)) == expected


def _edges(*shares: tuple[int, int]) -> list[int]:
    """An edge luminance histogram with ``count`` pixels in each ``(bin, count)``."""
    histogram = [0] * 256
    for luminance, count in shares:
        histogram[luminance] += count
    return histogram


def _contrast_on(level: int, luminance: int) -> float:
    return images._contrast(luminance / 255, images._linear(level))


def test_content_in_one_colour_always_gets_3_to_1() -> None:
    for luminance in range(256):
        level = images._background_level(_edges((luminance, 100)))
        assert _contrast_on(level, luminance) >= 3, (luminance, level)


def test_content_in_two_colours_keeps_both_whatever_their_shares() -> None:
    # Each luminance vanishes into one background at most, so one of the three keeps both
    # colours, and a word must not be given up for more contrast on the text around it.
    for many in range(256):
        for few in range(256):
            level = images._background_level(_edges((many, 1000), (few, 100)))
            kept = min(_contrast_on(level, many), _contrast_on(level, few))
            assert kept >= 1.5, (many, few, level)


def test_a_light_word_beside_much_dark_content_still_counts() -> None:
    # A stray allowance that grew with the image's edges counted a short white word beside a
    # page of dark text as a stray, so the background went white and the word vanished.
    img = _clear((400, 300))
    for y in range(2, 290, 12):
        for x in range(2, 390, 12):
            img.paste((0, 0, 0, 255), (x, y, x + 8, y + 8))
    img.paste((255, 255, 255, 255), (2, 2, 10, 10))
    assert _sent(_encoded(img)).getpixel((0, 0)) == (128, 128, 128)


def test_content_inside_other_content_takes_no_part() -> None:
    # Dark text on an opaque white card: only the card meets the transparent area.
    img = _clear((100, 60))
    img.paste((255, 255, 255, 255), (10, 10, 90, 50))
    img.paste((0, 0, 0, 255), (14, 14, 86, 46))
    assert _sent(_encoded(img)).getpixel((0, 0)) == (0, 0, 0)


def test_faint_pixels_take_no_part() -> None:
    # A soft shadow around white text is not content that has to stand out.
    img = _clear((100, 60), under=(0, 0, 0))
    img.paste((0, 0, 0, 60), (5, 5, 95, 55))
    img.paste((255, 255, 255, 255), (30, 20, 70, 40))
    assert _sent(_encoded(img)).getpixel((0, 0)) == (0, 0, 0)


def test_faint_content_alone_still_decides() -> None:
    img = _clear()
    img.paste((255, 255, 255, 100), (20, 10, 40, 20))
    assert _sent(_encoded(img)).getpixel((0, 0)) == (0, 0, 0)


def test_nothing_visible_is_flattened_onto_white() -> None:
    sent = _sent(_encoded(_clear(under=(255, 0, 0))))
    assert sent.getextrema() == ((255, 255), (255, 255), (255, 255))


def _grey_with_alpha() -> bytes:
    img = Image.new("LA", (60, 30), (255, 0))
    img.paste((255, 255), (20, 10, 40, 20))
    return _encoded(img)


def _palette_with_transparent_index() -> bytes:
    img = Image.new("P", (60, 30), 0)
    img.putpalette([255, 0, 0, 255, 255, 255])
    img.paste(1, (20, 10, 40, 20))
    return _encoded(img, transparency=0)


def _colour_key() -> bytes:
    img = Image.new("RGB", (60, 30), (255, 0, 0))
    img.paste((255, 255, 255), (20, 10, 40, 20))
    return _encoded(img, transparency=(255, 0, 0))


def _grey_colour_key() -> bytes:
    img = Image.new("L", (60, 30), 100)
    img.paste(255, (20, 10, 40, 20))
    return _encoded(img, transparency=100)


def _webp_with_alpha() -> bytes:
    img = _clear(under=(255, 0, 0))
    img.paste((255, 255, 255, 255), (20, 10, 40, 20))
    return _encoded(img, "WEBP", lossless=True, exact=True)


@pytest.mark.parametrize(
    ("picture", "mode"),
    [
        (_grey_with_alpha, "L"),
        (_palette_with_transparent_index, "P"),
        (_colour_key, "RGB"),
        (_grey_colour_key, "L"),
        (_webp_with_alpha, "RGB"),
    ],
)
def test_every_kind_of_transparency_is_flattened(picture: Any, mode: str) -> None:
    # Grey stays grey and a palette stays a palette, so the stored copy doesn't grow.
    data = picture()
    sent = _sent(data)
    assert sent.format == Image.open(BytesIO(data)).format, "its media type still holds"
    assert sent.mode == mode
    assert "transparency" not in sent.info
    assert sent.convert("RGB").getpixel((0, 0)) == (0, 0, 0)
    assert sent.convert("RGB").getpixel((30, 15)) == (255, 255, 255)


def test_a_palette_with_an_opacity_per_entry_is_composited_entry_by_entry() -> None:
    # The form quantisers write.  The red under the transparent entry must not show, and a
    # half-opaque white entry lands halfway between white and the background.
    img = Image.new("P", (60, 30), 0)
    img.putpalette([255, 0, 0, 255, 255, 255, 255, 255, 255])
    img.paste(1, (5, 5, 25, 25))
    img.paste(2, (35, 5, 55, 25))
    sent = _sent(_encoded(img, transparency=bytes([0, 255, 128])))
    assert sent.mode == "P"
    flat = sent.convert("RGB")
    assert flat.getpixel((0, 0)) == (0, 0, 0)
    assert flat.getpixel((15, 15)) == (255, 255, 255)
    assert flat.getpixel((45, 15)) == (128, 128, 128)


def test_a_palette_keeps_its_indices() -> None:
    # Turned into RGB, a flattened palette image came out half as big again.
    img = _clear((240, 120))
    for i, colour in enumerate([(230, 60, 40), (40, 90, 200), (255, 255, 255), (20, 20, 20)]):
        img.paste((*colour, 255), (10 + 55 * i, 20, 55 + 55 * i, 100))
    data = _encoded(img.quantize(colors=8, method=Image.Quantize.FASTOCTREE))
    original = Image.open(BytesIO(data))
    sent = _sent(data)
    assert sent.mode == "P"
    assert sent.tobytes() == original.tobytes()
    assert len(flatten_image(data, max_bytes=_CAP)) <= len(data) * 1.1


def test_a_flattened_image_over_the_cap_is_stored_unflattened() -> None:
    # A stored image goes out with every later request, so it must stay within the cap.
    img = _clear()
    img.paste((255, 255, 255, 255), (20, 10, 40, 20))
    data = _encoded(img)
    flattened = flatten_image(data, max_bytes=_CAP)
    assert flattened != data
    assert flatten_image(data, max_bytes=len(flattened) - 1) is data


def _count_loads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    from PIL import ImageFile

    loads: list[int] = []
    real = ImageFile.ImageFile.load

    def counting(self: Any) -> Any:
        loads.append(1)
        return real(self)

    monkeypatch.setattr(ImageFile.ImageFile, "load", counting)
    return loads


def test_an_image_over_the_flatten_cap_is_left_as_uploaded_undecoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Flattening holds several full-size copies at once, so a large image is never decoded.
    img = _clear()
    img.paste((255, 255, 255, 255), (20, 10, 40, 20))
    data = _encoded(img)
    monkeypatch.setattr(images, "_FLATTEN_MAX_PIXELS", 60 * 30 - 1)
    loads = _count_loads(monkeypatch)
    assert flatten_image(data, max_bytes=_CAP) is data
    assert loads == []


@pytest.mark.parametrize(
    ("bit_depth", "colour_type", "row", "trns"),
    [
        (16, 0, bytes(8) + bytes([128, 0] * 4), bytes([0, 0])),
        (16, 2, bytes(24) + bytes([128, 0, 64, 0, 32, 0] * 4), bytes(6)),
        (4, 0, bytes([255, 255, 0, 0]), bytes([0, 15])),
        (2, 0, bytes([255, 0]), bytes([0, 3])),
        (1, 0, bytes([240]), bytes([0, 1])),
    ],
)
def test_a_colour_key_pillow_cannot_apply_exactly_is_left_as_uploaded(
    bit_depth: int, colour_type: int, row: bytes, trns: bytes
) -> None:
    # Pillow matches a colour key against 8-bit samples, so a 16-bit image's key meets clipped or
    # truncated values and a 1-, 2- or 4-bit image's keeps the file's scale.  Flattening those
    # stored blank or wrongly keyed copies; they stay as uploaded instead.
    data = keyed_png(bit_depth, colour_type, [row] * 8, trns)
    assert "transparency" in Image.open(BytesIO(data)).info
    assert flatten_image(data, max_bytes=_CAP) is data


def test_a_colour_key_behind_a_misplaced_header_is_left_as_uploaded() -> None:
    # Pillow also opens a PNG whose first chunk isn't IHDR, and a byte of that chunk then sits
    # where IHDR's bit depth would.  Read as 8-bit, this 16-bit key was flattened wrongly.
    keyed = keyed_png(16, 2, [bytes(24) + bytes([128, 0, 64, 0, 32, 0] * 4)] * 8, bytes(6))
    body = b"Comment" + bytes([0, 8, 8, 8, 8])
    text = struct.pack(">L", len(body)) + b"tEXt" + body
    data = keyed[:8] + text + struct.pack(">L", zlib.crc32(b"tEXt" + body)) + keyed[8:]
    assert data[24] == 8
    assert "transparency" in Image.open(BytesIO(data)).info
    assert flatten_image(data, max_bytes=_CAP) is data


@pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
def test_an_animated_image_is_left_as_uploaded(fmt: str) -> None:
    # Flattening saves one frame, so the stored copy would stop moving.
    frames = []
    for shade in (0, 255):
        frame = _clear()
        frame.paste((shade, shade, shade, 255), (20, 10, 40, 20))
        frames.append(frame)
    buf = BytesIO()
    # Pillow writes a lossy animated WebP without its alpha, so it would not reach the check.
    params: dict[str, Any] = {"lossless": True} if fmt == "WEBP" else {}
    frames[0].save(buf, format=fmt, save_all=True, append_images=frames[1:], **params)
    data = buf.getvalue()
    opened = Image.open(BytesIO(data))
    assert opened.is_animated and images.has_transparency(opened)
    assert flatten_image(data, max_bytes=_CAP) is data


@pytest.mark.parametrize(("fmt", "params"), [("PNG", {}), ("WEBP", {"lossless": True})])
def test_a_flattened_image_keeps_its_colour_profile(fmt: str, params: dict[str, Any]) -> None:
    # Without its profile a wide-gamut image's colours wash out wherever colour is managed.
    profile = b"a colour profile, passed through as it is"
    img = _clear()
    img.paste((255, 255, 255, 255), (20, 10, 40, 20))
    sent = _sent(_encoded(img, fmt, icc_profile=profile, **params))
    assert sent.mode == "RGB"
    assert sent.info.get("icc_profile") == profile


@pytest.mark.parametrize("lossless", [True, False])
def test_a_flattened_webp_keeps_its_compression(lossless: bool) -> None:
    # A lossless WebP of text re-encoded lossy picked up artefacts and grew.
    img = _clear(under=(255, 0, 0))
    img.paste((255, 255, 255, 255), (20, 10, 40, 20))
    data = _encoded(img, "WEBP", lossless=lossless)
    flattened = flatten_image(data, max_bytes=_CAP)
    assert flattened != data
    assert images._webp_is_lossless(data) is lossless
    assert images._webp_is_lossless(flattened) is lossless


def test_a_one_frame_lossless_animation_stays_lossless() -> None:
    # A one-frame animation isn't animated, so it is flattened, and its image data sits inside the
    # frame's chunk.  Read from the top-level chunks alone, it was re-encoded lossy.
    frames = [_clear(), _clear()]
    for frame, shade in zip(frames, (255, 0), strict=True):
        frame.paste((shade, shade, shade, 255), (20, 10, 40, 20))
    two = _encoded(frames[0], "WEBP", save_all=True, append_images=frames[1:], lossless=True)
    chunks, pos = [], 12
    while pos + 8 <= len(two):
        size = int.from_bytes(two[pos + 4 : pos + 8], "little")
        chunks.append(two[pos : pos + 8 + size + (size & 1)])
        pos += 8 + size + (size & 1)
    frame_chunks = [chunk for chunk in chunks if chunk.startswith(b"ANMF")]
    body = b"WEBP" + b"".join(chunk for chunk in chunks if chunk is not frame_chunks[1])
    data = b"RIFF" + struct.pack("<L", len(body)) + body
    opened = Image.open(BytesIO(data))
    assert len(frame_chunks) == 2 and not opened.is_animated and images.has_transparency(opened)
    flattened = flatten_image(data, max_bytes=_CAP)
    assert flattened != data
    assert images._webp_is_lossless(data)
    assert images._webp_is_lossless(flattened)


def test_a_webp_naming_its_lossless_chunk_in_metadata_still_reads_as_lossy() -> None:
    # The chunk name can appear in metadata ahead of the image data, so it is found by walking
    # the chunks, not by searching the bytes.
    img = _clear()
    img.paste((255, 255, 255, 255), (20, 10, 40, 20))
    data = _encoded(img, "WEBP", quality=90, icc_profile=b"VP8L" * 4)
    assert b"VP8L" in data
    assert not images._webp_is_lossless(data)


@pytest.mark.filterwarnings("ignore:IM image format is deprecated")
def test_bytes_that_sniff_as_webp_are_not_read_as_another_format() -> None:
    # Some of Pillow's decoders accept any bytes, so a file with WebP's magic and an IM header
    # was read as IM, flattened and stored as an IM file.
    crlf = bytes([13, 10])
    header = b"RIFF" + bytes([16, 0, 0, 0]) + b"WEBP: x" + crlf
    header += b"Image type: RGBA image" + crlf + b"Image size (x*y): 4*4" + crlf
    planes = b"".join(bytes([255] * 12) + bytes([255, 0, 0, 255]) for _ in range(4))
    data = header + bytes([26]) + planes
    Image.init()
    if "IM" in Image.OPEN:  # while Pillow still has the decoder, an unrestricted open uses it
        assert Image.open(BytesIO(data)).format == "IM"
    assert flatten_image(data, max_bytes=_CAP) is data


def test_images_are_flattened_one_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each flatten holds several full-size copies, so concurrent ones must not stack.
    import threading
    import time

    running, most = [0], [0]
    lock = threading.Lock()
    real = images.flatten_transparency

    def slow(img: Any) -> Any:
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return real(img)

    monkeypatch.setattr(images, "flatten_transparency", slow)
    img = _clear()
    img.paste((255, 255, 255, 255), (20, 10, 40, 20))
    data = _encoded(img)
    workers = [
        threading.Thread(target=flatten_image, args=(data,), kwargs={"max_bytes": _CAP})
        for _ in range(3)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
    assert most[0] == 1


def test_erosion_matches_a_3x3_minimum() -> None:
    # The box blur stands in for MinFilter(3), which holds the GIL while it runs.
    import random

    from PIL import ImageFilter

    rng = random.Random(1445)
    for _ in range(200):
        size = (rng.randint(1, 24), rng.randint(1, 24))
        mask = Image.frombytes(
            "L", size, bytes(rng.choice((0, 255)) for _ in range(size[0] * size[1]))
        )
        assert images._erode(mask).tobytes() == mask.filter(ImageFilter.MinFilter(3)).tobytes()


def test_an_opaque_alpha_channel_is_kept_verbatim() -> None:
    # Screenshots often carry an alpha channel with nothing transparent in it.
    data = _encoded(Image.new("RGBA", (30, 20), (40, 80, 120, 255)))
    assert flatten_image(data, max_bytes=_CAP) is data


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_an_image_declaring_no_transparency_is_never_decoded(
    fmt: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    loads = _count_loads(monkeypatch)
    data = _encoded(Image.new("RGB", (30, 20), (40, 80, 120)), fmt)
    assert flatten_image(data, max_bytes=_CAP) is data
    assert loads == []


def test_what_needs_flattening_is_read_from_the_header(monkeypatch: pytest.MonkeyPatch) -> None:
    # Uploads with nothing to flatten skip the wait for a turn on this answer.
    transparent = _clear()
    transparent.paste((255, 255, 255, 255), (20, 10, 40, 20))
    big = _encoded(Image.new("LA", (60, 30)))
    loads = _count_loads(monkeypatch)
    assert needs_flattening(_encoded(transparent))
    # True while only a decode can tell: an alpha channel that may be wholly opaque, and a header
    # Pillow can't read, which flatten_image then keeps as it is.
    assert needs_flattening(_encoded(Image.new("RGBA", (30, 20), (40, 80, 120, 255))))
    assert needs_flattening(b"not an image")
    for fmt in ("PNG", "JPEG", "WEBP"):
        assert not needs_flattening(_encoded(Image.new("RGB", (30, 20)), fmt))
    monkeypatch.setattr(images, "_FLATTEN_MAX_PIXELS", 60 * 30 - 1)
    assert not needs_flattening(big)
    assert loads == []


def test_garbage_is_kept_verbatim() -> None:
    assert flatten_image(b"not an image", max_bytes=_CAP) == b"not an image"
    assert flatten_image(b"", max_bytes=_CAP) == b""


def test_a_turned_transparent_image_is_turned_and_flattened() -> None:
    img = _clear((40, 20))
    img.paste((255, 255, 255, 255), (0, 0, 20, 20))  # orientation 6 shows the left half on top
    sent = _sent(png_with_exif(exif_block([orientation_entry(6)]), img))
    assert (sent.size, sent.mode) == ((20, 40), "RGB")
    assert sent.getpixel((10, 5)) == (255, 255, 255)
    assert sent.getpixel((10, 35)) == (0, 0, 0)


@pytest.mark.parametrize(("gamma", "expected"), [(0.4, (0, 0, 0)), (2.5, (255, 255, 255))])
def test_edges_in_every_grey_go_where_fewest_blend_in(
    gamma: float, expected: tuple[int, int, int]
) -> None:
    # A bar shading from black to white has edges at every level, so no background is clear of
    # them.  Skewed light, few edges are dark; skewed dark, few are light.  Mid grey, nearest
    # the most edges either way, would lose the most.
    img = _clear((256, 30))
    for x in range(256):
        level = round(255 * (x / 255) ** gamma)
        img.paste((level, level, level, 255), (x, 10, x + 1, 20))
    assert _sent(_encoded(img)).getpixel((0, 0)) == expected


def test_every_image_decoder_checks_scans_and_reads_exif_itself() -> None:
    # A decoder that skips the scan check, or reads EXIF through Pillow, still
    # works on every ordinary file, so nothing else would notice.
    root = Path(__file__).resolve().parent.parent / "turnstone"
    checked: dict[str, bool] = {}
    pillow_exif: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        where = path.relative_to(root.parent)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("getexif", "exif_transpose")
            ):
                pillow_exif.append(f"{where}:{node.lineno}")
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            nodes = list(ast.walk(fn))
            opens = any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "open"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "Image"
                for n in nodes
            )
            if opens:
                names = {n.id for n in nodes if isinstance(n, ast.Name)}
                checked[f"{where}:{fn.name}"] = "too_many_jpeg_scans" in names
    assert "turnstone/core/images.py:normalize_image_orientation" in checked, checked
    assert all(checked.values()), checked
    assert pillow_exif == []


def test_every_image_entry_path_flattens() -> None:
    # Images are flattened as they enter a conversation, on each path separately.  A path that
    # stages an upload, or builds a read_file image part, without flattening would bring #1445
    # back for that path alone, and nothing else would notice.
    root = Path(__file__).resolve().parent.parent / "turnstone"
    entries: dict[str, bool] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        where = path.relative_to(root.parent)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            nodes = list(ast.walk(fn))
            called = {
                node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                for node in nodes
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute | ast.Name)
            }
            names = {node.id for node in nodes if isinstance(node, ast.Name)}
            if "stage" in called:
                entries[f"{where}:{fn.name}"] = bool(
                    {"stored_upload_content", "gated_upload_content"} & names
                )
            if "_encode_image_data_uri" in called:
                entries[f"{where}:{fn.name}"] = "flatten_image" in names
            if isinstance(fn, ast.AsyncFunctionDef) and "validate_and_save_uploaded_files" in names:
                # Saving flattens, so a handler holds its surface's gate while a worker thread
                # saves; without it, the waits for the flatten slot fill the default executor.
                gated = {
                    node.id
                    for block in nodes
                    if isinstance(block, ast.AsyncWith)
                    and any(
                        "flatten_gate" in ast.unparse(item.context_expr) for item in block.items
                    )
                    for node in ast.walk(block)
                    if isinstance(node, ast.Name)
                }
                entries[f"{where}:{fn.name} gate"] = "validate_and_save_uploaded_files" in gated
                # ...only when an image may need flattening, so a create of documents can't
                # queue behind someone else's images.
                entries[f"{where}:{fn.name} gate when needed"] = (
                    "any_upload_needs_flattening" in names
                )
    assert {
        "turnstone/core/attachments.py:validate_and_save_uploaded_files",
        "turnstone/core/session_routes.py:upload",
        "turnstone/core/session_routes.py:create gate",
        "turnstone/core/session_routes.py:create gate when needed",
        "turnstone/core/session.py:_exec_read_image",
    } <= set(entries), entries
    assert all(entries.values()), entries
