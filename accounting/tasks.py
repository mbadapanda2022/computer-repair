# accounting/tasks.py
import logging

from cloudinary.uploader import upload
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)


def upload_to_cloudinary(file_path, public_id):
    """
    Upload a file to Cloudinary.

    On error: log the exception and return None so callers can handle
    the failure gracefully instead of crashing.
    """
    try:
        with default_storage.open(file_path, 'rb') as file:
            result = upload(file, public_id=public_id)
            return result.get('secure_url')
    except Exception:
        logger.exception(
            "Cloudinary upload failed | path=%s | public_id=%s",
            file_path, public_id,
        )
        return None