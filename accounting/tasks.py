# accounting/tasks.py
from cloudinary.uploader import upload
from django.core.files.storage import default_storage

def upload_to_cloudinary(file_path, public_id):
    """
    Cloudinary पर फाइल अपलोड करने का Background Task
    """
    try:
        with default_storage.open(file_path, 'rb') as file:
            result = upload(file, public_id=public_id)
            return result.get('secure_url')
    except Exception as e:
        # Log the error (Render Logs में दिखेगा)
        print(f"🔥 Cloudinary Upload Error for {file_path}: {e}")
        return None