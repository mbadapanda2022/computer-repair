# accounting/validators.py
import os
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from PIL import Image

# Allowed extensions (Level 1 & 2)
ALLOWED_EXTENSIONS = ['jpg', 'jpeg', 'png', 'webp']

def validate_image_file_extension(value):
    """Level 2: Django's built-in FileExtensionValidator"""
    validator = FileExtensionValidator(allowed_extensions=ALLOWED_EXTENSIONS)
    validator(value)

def validate_image_binary(value):
    """
    Level 3: Deep binary validation using Pillow (Image.verify())
    Raises ValidationError if file is not a valid image.
    """
    try:
        # Open the file from the uploaded object
        with Image.open(value) as img:
            img.verify()  # Verifies file integrity, doesn't decode pixels
    except Exception as e:
        raise ValidationError(f"Invalid image file: {e}")

    # Optionally, you can also check the format after verify (but verify closes the file)
    # We need to reopen to get format
    try:
        value.seek(0)  # Reset file pointer after verify
        with Image.open(value) as img:
            format = img.format.lower()
            if format not in ALLOWED_EXTENSIONS:
                raise ValidationError(f"Unsupported image format: {format}")
    except Exception:
        # If we can't open it, it's invalid
        raise ValidationError("Unable to read image file.")
    return value