"""Tests for turnstone.core.avatars: the initial, the colour, and picture normalisation."""

from __future__ import annotations

import ast
import hashlib
import itertools
import math
import re
import struct
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from tests._exif_helpers import exif_block, jpeg_with_exif, orientation_entry, png_with_exif
from tests._helpers import keyed_png
from turnstone.core import avatars
from turnstone.core.avatars import (
    PALETTE_SIZE,
    PICTURE_SIZE,
    PictureRejectedError,
    assign_avatar_color,
    descriptor,
    effective_color,
    hashed_color,
    initial_for,
    normalize_picture,
    pick_color,
    picture_version,
)


def _s(*code_points: int) -> str:
    return "".join(chr(cp) for cp in code_points)


FOX = _s(0x1F98A)
SHARP_S = _s(0xDF)


class TestInitialFor:
    @pytest.mark.parametrize(
        ("display_name", "username", "expected"),
        [
            ("Jin Park", "jin", "J"),
            ("", "jin", "J"),
            ("   ", "jo", "J"),
            (_s(0x738B, 0x5C0F, 0x660E), "", _s(0x738B)),  # first ideograph
            (_s(0x3055, 0x304F, 0x3089), "", _s(0x3055)),  # first kana
            (_s(0xAE40, 0xBBFC, 0xC218), "", _s(0xAE40)),  # whole Hangul syllable
            (_s(0x43E, 0x43B, 0x44C, 0x433, 0x430), "", _s(0x41E)),  # Cyrillic, capitalised
            (_s(0x641, 0x627, 0x637, 0x645, 0x629), "", _s(0x641)),  # Arabic, no case
            # Devanagari conjunct syllable: the first consonant only.
            (_s(0x915, 0x94D, 0x937, 0x93F, 0x924, 0x93F, 0x91C), "", _s(0x915)),
            (_s(0x3B5, 0x3BB, 0x3AD, 0x3BD, 0x3B7), "", _s(0x395)),  # Greek, capitalised
            (FOX + " Fox Lee", "", "F"),  # emoji skipped
            ("(ana)", "", "A"),  # punctuation skipped
            ("@mara", "", "M"),
            ("1" + _s(0xFE0F, 0x20E3) + " Team", "", "1"),  # keycap reduced to its digit
            (_s(0x1D504, 0x1D52B, 0x1D52B, 0x1D51E), "", "A"),  # fraktur becomes plain A
            (_s(0xFF21, 0xFF4E, 0xFF41), "", "A"),  # full-width becomes plain A
            (SHARP_S + "tefan", "", SHARP_S),  # the capital would be two letters
            ("ilker", "", "I"),  # locale-free capital
            ("e" + _s(0x301) + "mile", "", _s(0xC9)),  # composed by NFKC
            # Georgian: titlecase keeps the ordinary letter, where uppercase
            # would give the all-capitals (Mtavruli) form.
            (_s(0x10D2, 0x10D8, 0x10DD, 0x10E0, 0x10D2, 0x10D8), "", _s(0x10D2)),
            # Thai and Lao vowels written before their consonant are skipped.
            (_s(0x0E40, 0x0E08, 0x0E19), "", _s(0x0E08)),
            (_s(0x0E44, 0x0E1E, 0x0E25, 0x0E34, 0x0E19), "", _s(0x0E1E)),
            (_s(0x0EC0, 0x0E9A, 0x0EB4, 0x0E99), "", _s(0x0E9A)),
            (_s(0x02BB) + "Iolani", "", "I"),  # the okina, a modifier letter, is skipped
            (_s(0xA015, 0xA00A), "", _s(0xA015)),  # a modifier letter outside that block stays
        ],
    )
    def test_letter(self, display_name: str, username: str, expected: str) -> None:
        assert initial_for(display_name, username) == expected

    def test_no_letter_anywhere_is_empty(self) -> None:
        assert initial_for(FOX + FOX, "___") == ""


class TestColour:
    @pytest.mark.parametrize(
        ("user_id", "expected"),
        [
            ("0123456789abcdef0123456789abcdef", 3),
            ("ffffffffffffffffffffffffffffffff", 0),
            ("5f3b2a90c4d14e6f8a7b9c0d1e2f3a4b", 1),
            ("channel-gateway", 4),
        ],
    )
    def test_hashed_color_is_stable(self, user_id: str, expected: int) -> None:
        digest = hashlib.sha256(user_id.encode()).digest()
        assert int.from_bytes(digest[:4], "big") % PALETTE_SIZE == expected
        assert hashed_color(user_id) == expected

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [(2, 2), (0, 0), (5, 5), (None, 3), (6, 3), (-1, 3), (True, 3), ("2", 3)],
    )
    def test_effective_color(self, stored: Any, expected: int) -> None:
        assert effective_color("0123456789abcdef0123456789abcdef", stored) == expected

    def test_pick_with_nobody_else_is_the_hashed_colour(self) -> None:
        uid = "0123456789abcdef0123456789abcdef"
        assert pick_color(uid, "J", []) == hashed_color(uid)

    def test_pick_avoids_a_colour_someone_with_the_initial_has(self) -> None:
        uid = "0123456789abcdef0123456789abcdef"
        taken = hashed_color(uid)
        picked = pick_color(uid, "J", [("J", taken)])
        assert picked != taken
        # And it avoids colours a colour-blind viewer would confuse with the taken one.
        assert frozenset((picked, taken)) not in avatars._CONFUSABLE

    def test_pick_ignores_other_initials(self) -> None:
        uid = "0123456789abcdef0123456789abcdef"
        others = [("A", c) for c in range(PALETTE_SIZE)]
        assert pick_color(uid, "J", others) == hashed_color(uid)

    def test_pick_when_every_colour_is_taken_once(self) -> None:
        # Each colour has one J; the least-confusable colour wins.  Orange (0)
        # merges with two others; every other colour with three or four.
        others = [("J", c) for c in range(PALETTE_SIZE)]
        assert pick_color("0123456789abcdef0123456789abcdef", "J", others) == 0

    def test_ties_go_round_the_palette_from_the_hashed_colour(self) -> None:
        uid = "0123456789abcdef0123456789abcdef"  # hashes to 3
        # Blue (3) is taken; the colours nobody would confuse with blue are
        # orange (0) and pink (5).  Going round from 3, pink comes first.
        assert pick_color(uid, "J", [("J", 3)]) == 5

    def test_people_with_no_letter_count_as_sharing_one(self) -> None:
        uid = "0123456789abcdef0123456789abcdef"
        assert pick_color(uid, "", [("", hashed_color(uid))]) != hashed_color(uid)


_BASE_CSS = Path(__file__).resolve().parent.parent / "turnstone/shared_static/base.css"

# Machado, Oliveira and Fernandes (2009) simulation matrices at full severity,
# applied to linear sRGB.
_MACHADO = (
    (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
    (
        (1.255528, -0.076749, -0.178779),
        (-0.078411, 0.930809, 0.147602),
        (0.004733, 0.691367, 0.303900),
    ),
)


def _oklch_to_linear_srgb(lightness: float, chroma: float, hue: float) -> list[float]:
    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return [
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    ]


def _linear_srgb_to_oklab(r: float, g: float, b: float) -> tuple[float, float, float]:
    def cbrt(x: float) -> float:
        return math.copysign(abs(x) ** (1 / 3), x)

    l_ = cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m_ = cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s_ = cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def _as_seen(oklch: tuple[float, float, float], matrix: Any) -> tuple[float, float, float]:
    rgb = [min(max(v, 0.0), 1.0) for v in _oklch_to_linear_srgb(*oklch)]
    seen = [min(max(sum(m * v for m, v in zip(row, rgb, strict=True)), 0.0), 1.0) for row in matrix]
    return _linear_srgb_to_oklab(*seen)


def test_confusable_table_matches_the_palette() -> None:
    # The dark theme's tokens come first in base.css, the light theme's second.
    themes: list[dict[tuple[int, str], tuple[float, float, float]]] = [{}, {}]
    pattern = r"--avatar-(\d)-(color|tint):\s*oklch\(([\d.]+)\s+([\d.]+)\s+([\d.]+)\)"
    for n, kind, lightness, chroma, hue in re.findall(pattern, _BASE_CSS.read_text("utf-8")):
        theme = themes[0] if (int(n), kind) not in themes[0] else themes[1]
        theme[int(n), kind] = (float(lightness), float(chroma), float(hue))
    assert [len(theme) for theme in themes] == [2 * PALETTE_SIZE, 2 * PALETTE_SIZE]
    found = set()
    for i, j in itertools.combinations(range(PALETTE_SIZE), 2):
        for theme, matrix in itertools.product(themes, _MACHADO):
            letters = math.dist(
                _as_seen(theme[i, "color"], matrix), _as_seen(theme[j, "color"], matrix)
            )
            tints = math.dist(
                _as_seen(theme[i, "tint"], matrix), _as_seen(theme[j, "tint"], matrix)
            )
            if letters < 0.10 and tints < 0.05:
                found.add(frozenset((i, j)))
    assert found == avatars._CONFUSABLE


class TestDescriptor:
    def test_without_a_row(self) -> None:
        uid = "0123456789abcdef0123456789abcdef"
        assert descriptor(uid, "Jin Park", "jin", None) == {
            "color": hashed_color(uid),
            "initial": "J",
            "picture": None,
        }

    def test_with_colour_and_picture(self) -> None:
        sha = "ab" * 32
        got = descriptor("u", "", "mara", {"color": 4, "image_sha256": sha})
        assert got == {"color": 4, "initial": "M", "picture": sha[:16]}

    def test_picture_version(self) -> None:
        assert picture_version(None) is None
        assert picture_version("") is None
        assert picture_version("0123456789abcdef0123") == "0123456789abcdef"


class _FakeStorage:
    def __init__(self, users: list[dict[str, str]], colors: dict[str, int]) -> None:
        self.users = users
        self.colors = colors

    def list_users(self) -> list[dict[str, str]]:
        return self.users

    def list_user_avatars(self) -> dict[str, dict[str, Any]]:
        return {uid: {"color": c, "image_sha256": None} for uid, c in self.colors.items()}

    def set_user_avatar_color(self, user_id: str, color: int) -> bool:
        self.colors[user_id] = color
        return True


def test_every_account_creation_picks_a_colour() -> None:
    # An account made without the pick still renders, in its hashed colour, so
    # nothing else would notice a creation path that skips it.
    root = Path(__file__).resolve().parent.parent / "turnstone"
    creating = {"create_user", "create_first_user", "create_oidc_user"}
    paired: dict[str, bool] = {}
    for path in sorted(root.rglob("*.py")):
        for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            nodes = list(ast.walk(fn))
            called = {
                n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                for n in nodes
                if isinstance(n, ast.Call)
            }
            if called & creating:
                names = {n.id for n in nodes if isinstance(n, ast.Name)}
                paired[f"{path.relative_to(root.parent)}:{fn.name}"] = (
                    "assign_avatar_color" in names
                )
    assert "turnstone/core/auth.py:handle_auth_setup" in paired, paired
    assert all(paired.values()), paired


class TestAssignAvatarColor:
    def test_new_account_avoids_the_colour_of_someone_with_its_initial(self) -> None:
        new_id = "0123456789abcdef0123456789abcdef"
        storage = _FakeStorage(
            [
                {"user_id": new_id, "username": "jo", "display_name": "Jo Chen"},
                {"user_id": "b" * 32, "username": "jin", "display_name": "Jin Park"},
                {"user_id": "c" * 32, "username": "ana", "display_name": "Ana Ruiz"},
            ],
            {"b" * 32: hashed_color(new_id)},
        )
        assign_avatar_color(storage, new_id, "Jo Chen", "jo")
        assert storage.colors[new_id] != hashed_color(new_id)

    def test_an_existing_user_without_a_row_counts_with_their_hashed_colour(self) -> None:
        new_id = "0123456789abcdef0123456789abcdef"
        # Find an id whose hashed colour equals the new account's.
        other = next(
            f"{n:032x}" for n in range(1000) if hashed_color(f"{n:032x}") == hashed_color(new_id)
        )
        storage = _FakeStorage(
            [
                {"user_id": new_id, "username": "jo", "display_name": "Jo"},
                {"user_id": other, "username": "jin", "display_name": "Jin"},
            ],
            {},
        )
        assign_avatar_color(storage, new_id, "Jo", "jo")
        assert storage.colors[new_id] != hashed_color(new_id)

    def test_a_storage_failure_leaves_the_hashed_colour(self) -> None:
        class Broken:
            def list_user_avatars(self) -> None:
                raise RuntimeError("down")

        assign_avatar_color(Broken(), "u", "Jo", "jo")  # must not raise

    def test_picks_run_one_at_a_time(self) -> None:
        # Two accounts made together must not both read everyone before either
        # writes: with the same initial and hashed colour they would match.
        first = "0123456789abcdef0123456789abcdef"
        second = next(
            f"{n:032x}" for n in range(1000) if hashed_color(f"{n:032x}") == hashed_color(first)
        )
        reading: list[int] = []
        overlap: list[int] = []

        class Slow(_FakeStorage):
            def list_users(self) -> list[dict[str, str]]:
                reading.append(1)
                overlap.append(len(reading))
                time.sleep(0.2)
                reading.pop()
                return self.users

        storage = Slow(
            [
                {"user_id": first, "username": "jo", "display_name": "Jo"},
                {"user_id": second, "username": "jin", "display_name": "Jin"},
            ],
            {},
        )
        picks = [
            threading.Thread(target=assign_avatar_color, args=(storage, uid, name, name.lower()))
            for uid, name in ((first, "Jo"), (second, "Jin"))
        ]
        for pick in picks:
            pick.start()
        for pick in picks:
            pick.join(10)
        assert overlap == [1, 1]
        assert storage.colors[first] != storage.colors[second]


def _png(size: tuple[int, int], color: tuple[int, ...] = (200, 30, 30, 255)) -> bytes:
    buf = BytesIO()
    Image.new("RGBA", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _open(png: bytes) -> Image.Image:
    img = Image.open(BytesIO(png))
    img.load()
    return img


class TestNormalizePicture:
    def test_png_becomes_a_square_png(self) -> None:
        png, sha = normalize_picture(_png((300, 200)))
        img = _open(png)
        assert img.format == "PNG"
        assert img.size == (PICTURE_SIZE, PICTURE_SIZE)
        assert sha == hashlib.sha256(png).hexdigest()

    def test_transparency_is_kept(self) -> None:
        png, _ = normalize_picture(_png((64, 64), (10, 20, 30, 0)))
        assert _open(png).convert("RGBA").getpixel((5, 5))[3] == 0

    def test_jpeg_and_webp_are_accepted(self) -> None:
        for fmt in ("JPEG", "WEBP"):
            buf = BytesIO()
            Image.new("RGB", (40, 60), (0, 128, 255)).save(buf, format=fmt)
            png, _ = normalize_picture(buf.getvalue())
            assert _open(png).size == (PICTURE_SIZE, PICTURE_SIZE)

    @pytest.mark.parametrize(
        ("fmt", "options"),
        [("JPEG", {"quality": 95}), ("WEBP", {"lossless": True}), ("PNG", {})],
    )
    def test_exif_orientation_is_applied_and_metadata_dropped(
        self, fmt: str, options: dict[str, Any]
    ) -> None:
        # Stored pixels: left half red, right half blue.  Orientation 6 means
        # "rotate 90 degrees clockwise to display", which puts red on top.
        img = Image.new("RGB", (40, 20), (255, 0, 0))
        img.paste((0, 0, 255), (20, 0, 40, 20))
        exif = Image.Exif()
        exif[0x0112] = 6
        exif[0x010F] = "ExampleCam"  # Make: camera metadata that must not survive
        buf = BytesIO()
        img.save(buf, format=fmt, exif=exif.tobytes(), **options)
        stored = _open(normalize_picture(buf.getvalue())[0])
        out = stored.convert("RGB")
        top = out.getpixel((PICTURE_SIZE // 2, 10))
        bottom = out.getpixel((PICTURE_SIZE // 2, PICTURE_SIZE - 10))
        assert top[0] > 200 and top[2] < 60
        assert bottom[2] > 200 and bottom[0] < 60
        assert "exif" not in stored.info

    def test_colour_profile_is_dropped(self) -> None:
        # An upload can label any bytes its colour profile; none are stored.
        from PIL import ImageCms

        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        buf = BytesIO()
        Image.new("RGB", (32, 32), (1, 2, 3)).save(buf, format="PNG", icc_profile=profile)
        assert "icc_profile" not in _open(normalize_picture(buf.getvalue())[0]).info

    def test_sixteen_bit_grey_is_scaled_not_clipped(self) -> None:
        buf = BytesIO()
        Image.new("I;16", (32, 32), 0x8000).save(buf, format="PNG")
        pixel = _open(normalize_picture(buf.getvalue())[0]).convert("RGBA").getpixel((16, 16))
        assert pixel == (128, 128, 128, 255)

    @pytest.mark.parametrize(
        ("mode", "background", "disc"), [("RGB", (0, 0, 0), (255, 255, 255)), ("L", 0, 255)]
    )
    def test_a_colour_key_gives_smooth_edges(self, mode: str, background: Any, disc: Any) -> None:
        # The key turns a colour transparent.  Applied after scaling, it would
        # miss the blended edge pixels and leave them as an opaque dark ring.
        img = Image.new(mode, (512, 512), background)
        ImageDraw.Draw(img).ellipse((64, 64, 448, 448), fill=disc)
        buf = BytesIO()
        img.save(buf, format="PNG", transparency=background)
        out = _open(normalize_picture(buf.getvalue())[0]).convert("RGBA")
        pixels = [out.getpixel((x, y)) for y in range(PICTURE_SIZE) for x in range(PICTURE_SIZE)]
        assert out.getpixel((2, 2))[3] == 0
        assert not [p for p in pixels if p[3] == 255 and p[0] < 200], "no opaque dark ring"
        assert [p for p in pixels if 0 < p[3] < 255], "the edge blends into transparency"

    def test_a_sixteen_bit_colour_key_is_scaled_with_the_pixels(self) -> None:
        img = Image.new("I;16", (64, 64), 0x8000)
        ImageDraw.Draw(img).rectangle((16, 16, 47, 47), fill=0)
        buf = BytesIO()
        img.save(buf, format="PNG", transparency=0x8000)
        out = _open(normalize_picture(buf.getvalue())[0]).convert("RGBA")
        assert out.getpixel((2, 2))[3] == 0, "the keyed background is transparent"
        assert out.getpixel((128, 128)) == (0, 0, 0, 255), "the black square is not"

    @pytest.mark.parametrize(
        ("depth", "colour_type", "row", "trns"),
        [
            # 16-bit grey: the key is 0x8000 and its neighbour 0x8001 stays opaque.
            (16, 0, struct.pack(">8H", *[0x8000] * 4, *[0x8001] * 4), struct.pack(">H", 0x8000)),
            # 2- and 4-bit grey, whose samples Pillow widens to 8 bits.
            (2, 0, bytes([0xFF, 0x00]), struct.pack(">H", 3)),
            (4, 0, bytes([0xFF, 0xFF, 0x00, 0x00]), struct.pack(">H", 15)),
            # 16-bit RGB, of which Pillow keeps the high bytes.
            (
                16,
                2,
                struct.pack(">24H", *[0x8000, 0x4000, 0x2000] * 4, *[0x00FF, 0x0012, 0x0034] * 4),
                struct.pack(">3H", 0x8000, 0x4000, 0x2000),
            ),
        ],
    )
    def test_a_colour_key_marks_its_own_pixels_at_every_depth(
        self, depth: int, colour_type: int, row: bytes, trns: bytes
    ) -> None:
        # The left half holds the key, the right half another value.
        data = keyed_png(depth, colour_type, [row] * 8, trns)
        out = _open(normalize_picture(data)[0]).convert("RGBA")
        assert out.getpixel((20, PICTURE_SIZE // 2))[3] == 0, "the keyed half is transparent"
        assert out.getpixel((PICTURE_SIZE - 20, PICTURE_SIZE // 2))[3] == 255, "the rest is not"

    @pytest.mark.parametrize(("fmt", "mode"), [("JPEG", "RGB"), ("PNG", "RGB"), ("PNG", "RGBA")])
    def test_the_centre_square_is_kept(self, fmt: str, mode: str) -> None:
        # Red, green and blue thirds: cropped, the sides are green; squashed,
        # they would be red and blue.
        img = Image.new(mode, (300, 100))
        draw = ImageDraw.Draw(img)
        for n, colour in enumerate([(255, 0, 0), (0, 255, 0), (0, 0, 255)]):
            draw.rectangle((n * 100, 0, n * 100 + 99, 99), fill=colour)
        buf = BytesIO()
        img.save(buf, format=fmt)
        out = _open(normalize_picture(buf.getvalue())[0]).convert("RGB")
        # An eighth of the way in from each side, clear of the blended edge.
        for x in (PICTURE_SIZE // 8, PICTURE_SIZE - PICTURE_SIZE // 8):
            red, green, blue = out.getpixel((x, PICTURE_SIZE // 2))
            assert green > 2 * max(red, blue), (x, (red, green, blue))

    def test_exif_after_a_pngs_pixels_is_ignored(self) -> None:
        # As in a browser, so the picture stores as the dialog showed it.
        png, _ = normalize_picture(
            png_with_exif(exif_block([orientation_entry(6)]), after_pixels=True)
        )
        out = _open(png).convert("RGB")
        left = out.getpixel((10, PICTURE_SIZE // 2))
        right = out.getpixel((PICTURE_SIZE - 10, PICTURE_SIZE // 2))
        assert left[0] > 200 and left[2] < 60, "red stays on the left"
        assert right[2] > 200 and right[0] < 60

    def test_oddly_typed_exif_is_still_turned(self) -> None:
        # Pillow's own transpose rewrites the remaining EXIF, and fails on a tag
        # stored in a type other than the one it expects.
        resolution_unit = (0x0128, 4, 1, struct.pack("<L", 70000))
        png, _ = normalize_picture(
            jpeg_with_exif(exif_block([orientation_entry(6), resolution_unit]))
        )
        top = _open(png).convert("RGB").getpixel((PICTURE_SIZE // 2, 10))
        assert top[0] > 200 and top[2] < 60

    def test_gif_is_rejected(self) -> None:
        buf = BytesIO()
        Image.new("RGB", (8, 8)).save(buf, format="GIF")
        with pytest.raises(PictureRejectedError, match="PNG, JPEG or WebP") as exc:
            normalize_picture(buf.getvalue())
        assert exc.value.status_code == 400

    def test_svg_is_rejected(self) -> None:
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"></svg>'
        with pytest.raises(PictureRejectedError, match="PNG, JPEG or WebP"):
            normalize_picture(svg)

    def test_too_many_bytes(self) -> None:
        with pytest.raises(PictureRejectedError, match="over 4 MB") as exc:
            normalize_picture(b"\x89PNG\r\n\x1a\n" + b"\0" * avatars.MAX_PICTURE_BYTES)
        assert exc.value.status_code == 413

    def test_limits(self) -> None:
        # Its own byte cap, not the attachment one, and pixel limits by how
        # each format decodes: PNG and WebP at full size, JPEG reduced.
        assert avatars.MAX_PICTURE_BYTES == 4 * 1024 * 1024
        assert (avatars._PICTURE_SIDE_LIMIT, avatars._JPEG_SIDE_LIMIT) == (2048, 4096)

    def test_too_many_pixels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(avatars, "_PICTURE_SIDE_LIMIT", 10)
        with pytest.raises(PictureRejectedError, match="too many pixels") as exc:
            normalize_picture(_png((20, 20)))
        assert exc.value.status_code == 413
        buf = BytesIO()
        Image.new("RGB", (20, 20)).save(buf, format="JPEG")
        assert normalize_picture(buf.getvalue())[0], "JPEG has its own, higher limit"
        monkeypatch.setattr(avatars, "_JPEG_SIDE_LIMIT", 10)
        with pytest.raises(PictureRejectedError, match="too many pixels"):
            normalize_picture(buf.getvalue())

    def test_the_pixel_limit_is_per_side(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # "N by N": a long, thin picture with fewer pixels in all is refused too.
        monkeypatch.setattr(avatars, "_PICTURE_SIDE_LIMIT", 10)
        with pytest.raises(PictureRejectedError, match="too many pixels"):
            normalize_picture(_png((11, 2)))
        assert normalize_picture(_png((10, 10)))[0]

    def test_jpeg_with_too_many_scans_is_refused(self) -> None:
        from tests._jpeg_helpers import jpeg_with_scans

        assert normalize_picture(jpeg_with_scans(0))[0]
        with pytest.raises(PictureRejectedError, match="passes") as exc:
            normalize_picture(jpeg_with_scans(150))
        assert exc.value.status_code == 400

    def test_large_jpeg_decodes_at_reduced_scale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        buf = BytesIO()
        Image.new("RGB", (2400, 1600), (30, 160, 90)).save(buf, format="JPEG")
        from PIL import JpegImagePlugin

        scaled: list[tuple[int, int]] = []
        real_draft = JpegImagePlugin.JpegImageFile.draft

        def spy(self: Any, mode: Any, size: Any) -> Any:
            result = real_draft(self, mode, size)
            scaled.append(self.size)
            return result

        monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)
        png, _ = normalize_picture(buf.getvalue())
        # 2400x1600 decodes at half scale; a quarter would leave the short side
        # under 512.
        assert scaled == [(1200, 800)]
        assert _open(png).size == (PICTURE_SIZE, PICTURE_SIZE)
        # A multi-picture JPEG, as some cameras write, decodes the same way.
        scaled.clear()
        mpo = BytesIO()
        first = Image.new("RGB", (2400, 1600), (30, 160, 90))
        first.save(mpo, format="MPO", save_all=True, append_images=[first.copy()])
        assert Image.open(BytesIO(mpo.getvalue())).format == "MPO"
        normalize_picture(mpo.getvalue())
        assert scaled == [(1200, 800)]

    def test_corrupt_image(self) -> None:
        with pytest.raises(PictureRejectedError, match="could not be read"):
            normalize_picture(b"\x89PNG\r\n\x1a\n" + b"not really a png at all")
