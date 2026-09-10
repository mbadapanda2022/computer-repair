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

    IMPORTANT: Har Image.open() ke baad cursor aage chala jata hai.
    Cloudinary ko poori file chahiye hoti hai, isliye return karne se
    PEHLE cursor ko 0 par reset karna ZAROORI hai.
    """
    try:
        # Step 1: File pointer ko start par le jao
        value.seek(0)

        # Step 2: PIL se verify karo
        with Image.open(value) as img:
            img.verify()

        # Step 3: Cursor reset (verify ne aage badha diya)
        value.seek(0)

        # Step 4: Format check
        with Image.open(value) as img:
            img_format = (img.format or '').lower()
            if img_format not in ALLOWED_EXTENSIONS:
                raise ValidationError(f"Unsupported image format: {img_format}")

        # Step 5: SABSE ZAROORI — return se PEHLE cursor reset
        value.seek(0)

    except ValidationError:
        # Validation error ko as-is raise karo, cursor reset karke
        value.seek(0)
        raise
    except Exception as e:
        # Koi bhi PIL/IO error -> cursor reset + ValidationError
        value.seek(0)
        raise ValidationError(f"Invalid image file: {e}")

    return value