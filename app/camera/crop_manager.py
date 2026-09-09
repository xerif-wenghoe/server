import math

from PIL import Image

from .app_config import load_crop_config


def rotate_clockwise(image: Image.Image, rotation: float) -> Image.Image:
    """Rotate by any angle from -180° to 180°, expanding to retain the image."""
    if not -180.0 <= rotation <= 180.0:
        raise ValueError("Rotation must be between -180° and 180°.")
    return image.rotate(-rotation, expand=True, resample=Image.Resampling.BICUBIC)


def apply_user_crop(image: Image.Image) -> Image.Image:
    """Apply the calibrated rectangular crop proportionally to a new image."""
    cfg = load_crop_config()
    if not cfg.get("enabled"):
        return image

    image = rotate_clockwise(image, float(cfg.get("rotation", 0)))
    source_w = int(cfg.get("source_width", 0))
    source_h = int(cfg.get("source_height", 0))
    if source_w <= 0 or source_h <= 0:
        raise ValueError("Invalid crop calibration dimensions.")

    scale_x = image.width / source_w
    scale_y = image.height / source_h
    x = max(0, round(int(cfg["x"]) * scale_x))
    y = max(0, round(int(cfg["y"]) * scale_y))
    right = min(image.width, x + round(int(cfg["width"]) * scale_x))
    bottom = min(image.height, y + round(int(cfg["height"]) * scale_y))
    if right <= x or bottom <= y:
        raise ValueError("Configured crop lies outside the uploaded image.")
    return image.crop((x, y, right, bottom))


def validate_crop(image, rotation, x, y, width, height):
    if image.width < 400 or image.height < 400:
        raise ValueError("The test image must be at least 400 × 400 pixels.")
    original_width, original_height = image.size
    rotated = rotate_clockwise(image, rotation)
    if width < 400 or height < 400:
        raise ValueError("The crop rectangle must be at least 400 × 400 pixels.")
    if x < 0 or y < 0 or x + width > rotated.width or y + height > rotated.height:
        raise ValueError("The crop rectangle must remain inside the rotated image.")

    # The expanded rotated canvas contains blank triangular corners. Map every
    # crop corner back into the original image and reject any blank-area overlap.
    angle = math.radians(rotation)
    cosine, sine = math.cos(angle), math.sin(angle)
    rotated_cx, rotated_cy = rotated.width / 2.0, rotated.height / 2.0
    original_cx, original_cy = original_width / 2.0, original_height / 2.0
    corners = ((x, y), (x + width, y), (x, y + height), (x + width, y + height))
    tolerance = 1.5
    for corner_x, corner_y in corners:
        dx, dy = corner_x - rotated_cx, corner_y - rotated_cy
        original_x = cosine * dx + sine * dy + original_cx
        original_y = -sine * dx + cosine * dy + original_cy
        if not (-tolerance <= original_x <= original_width + tolerance and
                -tolerance <= original_y <= original_height + tolerance):
            raise ValueError(
                "The crop rectangle must be fully inside the image and cannot include blank corners."
            )
    return rotated
