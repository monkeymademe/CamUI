"""Epson TM-T20II (and similar) photobooth thermal printing via ESC/POS USB."""

from __future__ import annotations

import logging
import os
import random
import threading
from datetime import datetime
from typing import Any, Callable, Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps

logger = logging.getLogger(__name__)

DEFAULT_VENDOR_ID = 0x04B8
DEFAULT_PRODUCT_ID = 0x0E15
DEFAULT_PAPER_WIDTH_PX = 576
PAPER_WIDTH_PRESETS = {
    80: 576,  # TM-T20II 80 mm printable width
    58: 420,  # TM-T20II 58 mm printable width (requires paper guide)
}
LOGO_POSITIONS = ("before_title", "after_title")
FUN_LINE_POSITIONS = ("before_photo", "after_photo")
DEFAULT_LOGO_MAX_HEIGHT = 120
DEFAULT_FUN_LINES = [
    "Looking good",
    "Nice one",
    "Perfect",
    "That's the shot",
    "Great smile",
    "Love it",
    "Absolute winner",
    "Pure magic",
    "Iconic",
    "Stunning",
    "One for the album",
    "That's a keeper",
    "Camera loves you",
    "Boom — got it",
    "Picture perfect",
    "What a moment",
    "Fabulous",
    "So good",
    "Beautiful",
    "Superb",
]
# Fixed on-disk logo used when logo_enabled is true.
DEFAULT_LOGO_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "static", "photobooth", "logo.png"
)
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

_print_lock = threading.Lock()
_queue_lock = threading.Lock()
_print_busy = False


def _parse_hex_id(value, default):
    if value is None or value == "":
        return default
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    try:
        return int(text, 16)
    except ValueError:
        return default


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def normalize_print_settings(raw=None):
    defaults = {
        "enabled": True,
        "title": "CamUI Photobooth",
        "show_date": True,
        "paper_width_mm": 80,
        "paper_width_px": DEFAULT_PAPER_WIDTH_PX,
        "density": 4,
        "cut_mode": "PART",
        "cut_feed_lines": 6,
        "logo_enabled": False,
        "logo_position": "before_title",
        "logo_scale_percent": 100,
        "logo_max_height": DEFAULT_LOGO_MAX_HEIGHT,
        "fun_lines": list(DEFAULT_FUN_LINES),
        "fun_line_print": True,
        "fun_line_position": "after_photo",
        "usb_vendor_id": "04b8",
        "usb_product_id": "0e15",
    }
    if not isinstance(raw, dict):
        return defaults.copy()

    merged = defaults.copy()
    merged["enabled"] = _as_bool(raw.get("enabled"), defaults["enabled"])
    title = str(raw.get("title", defaults["title"])).strip()[:160]
    merged["title"] = title or defaults["title"]
    merged["show_date"] = _as_bool(raw.get("show_date"), defaults["show_date"])

    # Prefer explicit mm preset; fall back to px for older configs.
    width_mm = raw.get("paper_width_mm", None)
    if width_mm is None and raw.get("paper_width_px") is not None:
        try:
            px = int(raw.get("paper_width_px"))
            width_mm = 58 if px <= 480 else 80
        except (TypeError, ValueError):
            width_mm = defaults["paper_width_mm"]
    try:
        width_mm = int(width_mm if width_mm is not None else defaults["paper_width_mm"])
    except (TypeError, ValueError):
        width_mm = defaults["paper_width_mm"]
    if width_mm not in PAPER_WIDTH_PRESETS:
        width_mm = defaults["paper_width_mm"]
    merged["paper_width_mm"] = width_mm
    merged["paper_width_px"] = PAPER_WIDTH_PRESETS[width_mm]

    try:
        density = int(raw.get("density", defaults["density"]))
        merged["density"] = max(0, min(8, density))
    except (TypeError, ValueError):
        merged["density"] = defaults["density"]

    cut_mode = str(raw.get("cut_mode", defaults["cut_mode"])).strip().upper()
    merged["cut_mode"] = cut_mode if cut_mode in ("FULL", "PART") else defaults["cut_mode"]

    try:
        feed = int(raw.get("cut_feed_lines", defaults["cut_feed_lines"]))
        merged["cut_feed_lines"] = max(0, min(20, feed))
    except (TypeError, ValueError):
        merged["cut_feed_lines"] = defaults["cut_feed_lines"]

    merged["logo_enabled"] = _as_bool(raw.get("logo_enabled"), defaults["logo_enabled"])
    logo_pos = str(raw.get("logo_position", defaults["logo_position"])).strip().lower()
    merged["logo_position"] = logo_pos if logo_pos in LOGO_POSITIONS else defaults["logo_position"]
    try:
        logo_h = int(raw.get("logo_max_height", defaults["logo_max_height"]))
        merged["logo_max_height"] = max(40, min(240, logo_h))
    except (TypeError, ValueError):
        merged["logo_max_height"] = defaults["logo_max_height"]
    try:
        scale_pct = int(raw.get("logo_scale_percent", defaults["logo_scale_percent"]))
        merged["logo_scale_percent"] = max(10, min(100, scale_pct))
    except (TypeError, ValueError):
        merged["logo_scale_percent"] = defaults["logo_scale_percent"]

    raw_lines = raw.get("fun_lines", defaults["fun_lines"])
    lines = []
    if isinstance(raw_lines, list):
        for item in raw_lines:
            text = str(item or "").strip()
            if text and text not in lines:
                lines.append(text[:80])
            if len(lines) >= 50:
                break
    merged["fun_lines"] = lines or list(DEFAULT_FUN_LINES)
    merged["fun_line_print"] = _as_bool(raw.get("fun_line_print"), defaults["fun_line_print"])
    fun_pos = str(raw.get("fun_line_position", defaults["fun_line_position"])).strip().lower()
    merged["fun_line_position"] = (
        fun_pos if fun_pos in FUN_LINE_POSITIONS else defaults["fun_line_position"]
    )

    merged["usb_vendor_id"] = (
        str(raw.get("usb_vendor_id", defaults["usb_vendor_id"])).strip()
        or defaults["usb_vendor_id"]
    )
    merged["usb_product_id"] = (
        str(raw.get("usb_product_id", defaults["usb_product_id"])).strip()
        or defaults["usb_product_id"]
    )
    return merged


def resolve_logo_path(settings=None):
    """Return the logo filesystem path when enabled and present."""
    settings = settings if isinstance(settings, dict) else {}
    if not _as_bool(settings.get("logo_enabled"), False):
        return None
    candidates = []
    raw_path = settings.get("logo_path")
    if isinstance(raw_path, str) and raw_path.strip():
        candidates.append(raw_path.strip())
    candidates.append(DEFAULT_LOGO_PATH)
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def is_printer_available(settings=None):
    settings = normalize_print_settings(settings)
    try:
        import usb.core
    except ImportError:
        return False

    vendor = _parse_hex_id(settings.get("usb_vendor_id"), DEFAULT_VENDOR_ID)
    product = _parse_hex_id(settings.get("usb_product_id"), DEFAULT_PRODUCT_ID)
    try:
        device = usb.core.find(idVendor=vendor, idProduct=product)
        return device is not None
    except Exception as e:
        logger.warning("USB printer probe failed: %s", e)
        return False


def _load_font(size, bold=False):
    path = FONT_BOLD_PATH if bold and os.path.isfile(FONT_BOLD_PATH) else FONT_PATH
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _wrap_text_lines(text, font, max_width, draw):
    """Word-wrap text to fit max_width. Long words are broken by character."""
    text = (text or "").strip()
    if not text:
        return []

    def text_w(value):
        bbox = draw.textbbox((0, 0), value, font=font)
        return bbox[2] - bbox[0]

    def break_word(word):
        if text_w(word) <= max_width:
            return [word]
        parts = []
        current = ""
        for ch in word:
            trial = current + ch
            if current and text_w(trial) > max_width:
                parts.append(current)
                current = ch
            else:
                current = trial
        if current:
            parts.append(current)
        return parts or [word]

    lines = []
    for paragraph in text.splitlines() or [text]:
        words = []
        for word in paragraph.split():
            words.extend(break_word(word))
        if not words:
            continue
        current = words[0]
        for word in words[1:]:
            trial = f"{current} {word}"
            if text_w(trial) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _text_block_metrics(lines, font, draw, line_gap=4):
    """Return (total_height, list of (line, width, height))."""
    metrics = []
    total_h = 0
    for index, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        line_h = bbox[3] - bbox[1]
        metrics.append((line, line_w, line_h))
        total_h += line_h
        if index:
            total_h += line_gap
    return total_h, metrics


def _draw_centered_text_block(draw, y, metrics, width, font, line_gap=4, fill="black"):
    for index, (line, line_w, line_h) in enumerate(metrics):
        if index:
            y += line_gap
        draw.text(((width - line_w) // 2, y), line, fill=fill, font=font)
        y += line_h
    return y


def _prepare_logo(logo_path, max_width, max_height=None):
    """Load a logo, flatten onto white, square-pad if needed, scale to full print width.

    Many circular logos are uploaded in a landscape frame that already clips the
    top/bottom of the circle; padding to a square restores proportions before
    scaling to the printable width.
    """
    if not logo_path or not os.path.isfile(logo_path):
        return None
    try:
        with Image.open(logo_path) as src:
            logo = ImageOps.exif_transpose(src)
            if logo.mode in ("RGBA", "LA") or (logo.mode == "P" and "transparency" in logo.info):
                rgba = logo.convert("RGBA")
                background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                logo = Image.alpha_composite(background, rgba).convert("RGB")
            else:
                logo = logo.convert("RGB")
    except Exception as e:
        logger.warning("Failed to load print logo %s: %s", logo_path, e)
        return None

    # Content bbox (near-white treated as background).
    mask = ImageOps.grayscale(logo).point(lambda p: 255 if p < 250 else 0)
    bbox = mask.getbbox()
    content = logo.crop(bbox) if bbox else logo

    # Square canvas so clipped circular logos keep round proportions.
    side = max(content.width, content.height)
    pad = max(8, side // 32)
    canvas_side = side + pad * 2
    canvas = Image.new("RGB", (canvas_side, canvas_side), "white")
    canvas.paste(
        content,
        ((canvas_side - content.width) // 2, (canvas_side - content.height) // 2),
    )
    logo = canvas

    max_width = max(8, int(max_width))
    max_width -= max_width % 8  # ESC/POS raster width multiple of 8
    if logo.width != max_width:
        scale = max_width / float(logo.width)
        new_h = max(1, int(round(logo.height * scale)))
        resample = getattr(Image, "Resampling", Image).LANCZOS
        logo = logo.resize((max_width, new_h), resample)

    # Dither after resize so colored marks survive monochrome thermal output.
    gray = ImageOps.grayscale(logo)
    gray = ImageOps.autocontrast(gray)
    dither = getattr(getattr(Image, "Dither", Image), "FLOYDSTEINBERG", Image.FLOYDSTEINBERG)
    return gray.convert("1", dither=dither).convert("RGB")


def build_receipt_image(
    photo_path,
    title,
    when=None,
    paper_width_px=DEFAULT_PAPER_WIDTH_PX,
    show_date=True,
    density=4,
    logo_path=None,
    logo_position="before_title",
    logo_scale_percent=100,
    fun_line=None,
    fun_line_position="after_photo",
):
    """Compose a receipt-width RGB image: logo, title, fun line, photo, date."""
    when = when or datetime.now()
    if isinstance(when, str):
        date_text = when
    else:
        date_text = when.strftime("%Y-%m-%d %H:%M")

    title = (title or "CamUI Photobooth").strip()
    fun_line = (fun_line or "").strip()
    width = int(paper_width_px)
    margin = 12
    gap = 10
    title_after_gap = 12  # breathing room under the brand line(s)
    show_date = bool(show_date)
    logo_position = logo_position if logo_position in LOGO_POSITIONS else "before_title"
    fun_line_position = (
        fun_line_position if fun_line_position in FUN_LINE_POSITIONS else "after_photo"
    )
    try:
        density = max(0, min(8, int(density)))
    except (TypeError, ValueError):
        density = 4
    try:
        logo_scale_percent = max(10, min(100, int(logo_scale_percent)))
    except (TypeError, ValueError):
        logo_scale_percent = 100

    with Image.open(photo_path) as src:
        photo = ImageOps.exif_transpose(src.convert("RGB"))

    max_photo_w = width - margin * 2
    scale = max_photo_w / float(photo.width)
    photo_h = max(1, int(round(photo.height * scale)))
    resample = getattr(Image, "Resampling", Image).LANCZOS
    photo = photo.resize((max_photo_w, photo_h), resample)

    # Map density 0..8 → gamma darkness (4 = neutral). Affects 1-bit threshold.
    if density != 4:
        mid = 4.0
        factor = 1.0 + ((density - mid) / mid) * 0.45
        factor = max(0.45, min(1.6, factor))
        lut = [min(255, int(round(pow(i / 255.0, factor) * 255.0))) for i in range(256)]
        photo = photo.point(lut * 3)

    logo_w = max(8, int(round(max_photo_w * (logo_scale_percent / 100.0))))
    logo_w -= logo_w % 8
    logo = _prepare_logo(logo_path, max_width=logo_w) if logo_path else None
    logo_h = logo.height if logo is not None else 0

    title_font = _load_font(28, bold=True)
    fun_font = _load_font(26, bold=True)
    date_font = _load_font(20, bold=False)

    probe = Image.new("RGB", (width, 10), "white")
    probe_draw = ImageDraw.Draw(probe)
    text_max_w = max_photo_w

    title_lines = _wrap_text_lines(title, title_font, text_max_w, probe_draw)
    if not title_lines:
        title_lines = ["CamUI Photobooth"]
    title_h, title_metrics = _text_block_metrics(title_lines, title_font, probe_draw)

    fun_h = 0
    fun_metrics = []
    if fun_line:
        fun_lines = _wrap_text_lines(fun_line, fun_font, text_max_w, probe_draw)
        fun_h, fun_metrics = _text_block_metrics(fun_lines, fun_font, probe_draw)

    date_h = 0
    date_bbox = (0, 0, 0, 0)
    if show_date:
        date_bbox = probe_draw.textbbox((0, 0), date_text, font=date_font)
        date_h = date_bbox[3] - date_bbox[1]

    header_h = title_h + title_after_gap
    if logo is not None:
        header_h += logo_h + gap

    fun_block_h = (gap + fun_h) if fun_line else 0

    total_h = margin + header_h + gap + photo_h + margin
    if fun_line:
        total_h += fun_block_h
    if show_date:
        total_h += gap + date_h
    receipt = Image.new("RGB", (width, total_h), "white")
    draw = ImageDraw.Draw(receipt)

    y = margin

    def paste_logo_at(top_y):
        logo_x = (width - logo.width) // 2
        receipt.paste(logo, (logo_x, top_y))
        return top_y + logo_h

    def draw_fun_at(top_y):
        return _draw_centered_text_block(draw, top_y, fun_metrics, width, fun_font)

    if logo is not None and logo_position == "before_title":
        y = paste_logo_at(y)
        y += gap

    y = _draw_centered_text_block(draw, y, title_metrics, width, title_font)
    y += title_after_gap

    if logo is not None and logo_position == "after_title":
        y += gap
        y = paste_logo_at(y)

    if fun_line and fun_line_position == "before_photo":
        y += gap
        y = draw_fun_at(y)

    y += gap
    receipt.paste(photo, (margin, y))
    y += photo_h

    if fun_line and fun_line_position == "after_photo":
        y += gap
        y = draw_fun_at(y)

    if show_date:
        date_x = (width - (date_bbox[2] - date_bbox[0])) // 2
        draw.text((date_x, y + gap), date_text, fill="black", font=date_font)

    return receipt


def _usb_ids(settings):
    vendor = _parse_hex_id(settings.get("usb_vendor_id"), DEFAULT_VENDOR_ID)
    product = _parse_hex_id(settings.get("usb_product_id"), DEFAULT_PRODUCT_ID)
    return vendor, product


def reset_usb_printer(settings=None):
    """Hard-reset the USB printer device (recovers after mid-job process kill)."""
    import time

    import usb.core
    import usb.util

    settings = normalize_print_settings(settings)
    vendor, product = _usb_ids(settings)
    dev = usb.core.find(idVendor=vendor, idProduct=product)
    if dev is None:
        return False
    try:
        if dev.is_kernel_driver_active(0):
            try:
                dev.detach_kernel_driver(0)
            except Exception:
                pass
    except Exception:
        pass
    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass
    try:
        dev.reset()
        print("Photobooth print: USB device reset", flush=True)
    except Exception as e:
        logger.warning("USB printer reset failed: %s", e)
        return False
    time.sleep(1.0)
    return True


def _open_usb_printer(settings):
    from escpos.printer import Usb

    vendor, product = _usb_ids(settings)
    # TM-T20II defaults: out 0x01, in 0x82 (python-escpos defaults).
    profile = "TM-T20II" if int(settings.get("paper_width_mm", 80)) == 80 else "TM-T20II"
    return Usb(vendor, product, timeout=0, profile=profile)


def _send_receipt(printer, receipt, settings):
    # ESC @ — initialize printer (clears partial job left by a killed process).
    printer._raw(b"\x1b\x40")
    printer.set(align="center")
    printer.image(
        receipt,
        high_density_vertical=True,
        high_density_horizontal=True,
        impl="bitImageRaster",
        center=True,
    )

    feed_lines = int(settings.get("cut_feed_lines", 6))
    if feed_lines > 0:
        printer.print_and_feed(feed_lines)

    cut_mode = str(settings.get("cut_mode", "PART")).upper()
    if cut_mode not in ("FULL", "PART"):
        cut_mode = "PART"
    printer.cut(mode=cut_mode, feed=False)


def print_photobooth_image(photo_path, settings=None, fun_line=None):
    """Synchronously print a photobooth receipt. Raises on failure."""
    settings = normalize_print_settings(settings)
    if not photo_path or not os.path.isfile(photo_path):
        raise FileNotFoundError(f"Photo not found: {photo_path}")

    if not is_printer_available(settings):
        raise RuntimeError("Epson TM-T20II not found on USB")

    logo_path = resolve_logo_path(settings)
    if settings.get("logo_enabled") and not logo_path:
        print("Photobooth print: logo enabled but file missing", flush=True)
    elif logo_path:
        print(f"Photobooth print: including logo {logo_path}", flush=True)

    print_fun = None
    if settings.get("fun_line_print"):
        print_fun = (fun_line or "").strip()
        if not print_fun:
            lines = settings.get("fun_lines") or DEFAULT_FUN_LINES
            if lines:
                print_fun = random.choice(lines)

    receipt = build_receipt_image(
        photo_path,
        settings.get("title"),
        when=datetime.now(),
        paper_width_px=settings.get("paper_width_px", DEFAULT_PAPER_WIDTH_PX),
        show_date=settings.get("show_date", True),
        density=settings.get("density", 4),
        logo_path=logo_path,
        logo_position=settings.get("logo_position", "before_title"),
        logo_scale_percent=settings.get("logo_scale_percent", 100),
        fun_line=print_fun,
        fun_line_position=settings.get("fun_line_position", "after_photo"),
    )

    with _print_lock:
        last_error = None
        for attempt in range(2):
            printer = None
            try:
                print(
                    f"Photobooth print: opening USB printer for {photo_path}"
                    + (f" (retry {attempt})" if attempt else ""),
                    flush=True,
                )
                printer = _open_usb_printer(settings)
                _send_receipt(printer, receipt, settings)
                print(f"Photobooth print: completed {photo_path}", flush=True)
                return True
            except Exception as e:
                last_error = e
                print(f"Photobooth print: attempt failed: {e}", flush=True)
                logger.exception("Photobooth print attempt failed")
                if attempt == 0:
                    reset_usb_printer(settings)
            finally:
                if printer is not None:
                    try:
                        printer.close()
                    except Exception:
                        pass
        raise last_error if last_error else RuntimeError("Print failed")


def queue_photobooth_print(
    photo_path: str,
    settings: Optional[dict] = None,
    on_done: Optional[Callable[[bool, Optional[str]], Any]] = None,
    wait: bool = False,
    fun_line: Optional[str] = None,
) -> dict:
    """
    Queue a background print job.

    Returns immediately with status (unless wait=True):
      {"queued": True} or {"queued": False, "error": "..."}
      When wait=True and print finishes: {"queued": True, "printed": True}
      When wait=True and print fails: {"queued": False, "error": "...", "printed": False}
    """
    global _print_busy
    settings = normalize_print_settings(settings)

    if not settings.get("enabled"):
        return {"queued": False, "printed": False, "error": "Photobooth printing is disabled"}

    if not os.path.isfile(photo_path):
        return {"queued": False, "printed": False, "error": "Photo file missing"}

    if not is_printer_available(settings):
        return {"queued": False, "printed": False, "error": "Printer not connected"}

    result = {"queued": True, "printed": False, "error": None}
    done = threading.Event()

    with _queue_lock:
        _print_busy = True

    def worker():
        global _print_busy
        error = None
        ok = False
        try:
            print_photobooth_image(photo_path, settings, fun_line=fun_line)
            ok = True
            result["printed"] = True
            print(f"Photobooth print completed: {photo_path}", flush=True)
        except Exception as e:
            error = str(e)
            result["error"] = error
            result["queued"] = False
            print(f"Photobooth print failed: {e}", flush=True)
            logger.exception("Photobooth print failed: %s", e)
        finally:
            with _queue_lock:
                _print_busy = False
            done.set()
            if on_done:
                try:
                    on_done(ok, error)
                except Exception:
                    logger.exception("Photobooth print on_done callback failed")

    if wait:
        # Run on this thread so Flask can return accurate print status.
        worker()
        return result

    thread = threading.Thread(
        target=worker,
        name="photobooth-print",
        daemon=True,
    )
    thread.start()
    return {"queued": True}
