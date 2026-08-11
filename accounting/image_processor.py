# accounting/image_processor.py
import io
import os
from PIL import Image
from django.core.files.uploadedfile import InMemoryUploadedFile

def process_uploaded_image(uploaded_file, max_size=(1200, 1200), quality=80):
    """
    Process an uploaded image:
      - Downscale to max_size using LANCZOS (high-quality)
      - Compress with quality 80%
      - Auto-detect transparency (RGBA) -> save as WebP, else JPEG
      - Return a new InMemoryUploadedFile with correct extension
    """
    # Open image from file-like object
    img = Image.open(uploaded_file)

    # Convert to RGBA if not already to check alpha
    if img.mode != 'RGBA':
        img = img.convert('RGBA')
    
    # Check if image has transparency (any pixel with alpha < 255)
    has_alpha = img.getchannel('A').getextrema() != (255, 255)

    # Convert to RGB if no alpha and format is JPEG (WebP also supports alpha)
    if not has_alpha:
        img = img.convert('RGB')

    # Downscale using LANCZOS
    img.thumbnail(max_size, Image.Resampling.LANCZOS)

    # Determine output format and extension
    if has_alpha:
        format = 'WEBP'
        ext = '.webp'
        # WebP supports transparency, we keep RGBA mode
        if img.mode != 'RGBA':
            img = img.convert('RGBA')
    else:
        format = 'JPEG'
        ext = '.jpg'
        if img.mode != 'RGB':
            img = img.convert('RGB')

    # Save to BytesIO
    output = io.BytesIO()
    img.save(output, format=format, quality=quality, optimize=True)
    output.seek(0)

    # Build new filename: change extension
    name, old_ext = os.path.splitext(uploaded_file.name)
    new_name = name + ext

    # Create a new InMemoryUploadedFile
    new_file = InMemoryUploadedFile(
        file=output,
        field_name=uploaded_file.field_name,
        name=new_name,
        content_type=f'image/{format.lower()}',
        size=output.getbuffer().nbytes,
        charset=uploaded_file.charset
    )
    return new_file