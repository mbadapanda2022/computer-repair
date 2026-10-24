# accounting/validators.py
import os
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from PIL import Image

# Allowed extensions (Level 1 & 2)
ALLOWED_EXTENSIONS = ['jpg', 'jpeg', 'png', 'webp']


def validate_image_file_extension(value):
    """Skip validation if file is None or has no name"""
    if not value or not hasattr(value, 'name') or not value.name:
        return
    validator = FileExtensionValidator(allowed_extensions=ALLOWED_EXTENSIONS)
    validator(value)


def validate_image_binary(value):
    """
    Level 3: Deep binary validation using Pillow (Image.verify()).

    IMPORTANT: The cursor advances after every Image.open().
    Cloudinary needs the whole file, so the cursor MUST be reset
    to 0 before returning.
    """
    try:
        # Step 1: Move the file pointer to the start
        value.seek(0)

        # Step 2: Verify with PIL
        with Image.open(value) as img:
            img.verify()

        # Step 3: Reset cursor (verify moved it forward)
        value.seek(0)

        # Step 4: Format check
        with Image.open(value) as img:
            img_format = (img.format or '').lower()
            if img_format not in ALLOWED_EXTENSIONS:
                raise ValidationError(f"Unsupported image format: {img_format}")

        # Step 5: MOST IMPORTANT — reset cursor before returning
        value.seek(0)

    except ValidationError:
        # Re-raise validation error as-is, after resetting the cursor
        value.seek(0)
        raise
    except Exception as e:
        # Any PIL/IO error -> cursor reset + ValidationError
        value.seek(0)
        raise ValidationError(f"Invalid image file: {e}")

    return value