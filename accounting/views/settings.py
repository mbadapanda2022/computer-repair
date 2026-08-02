# accounting/views/settings.py

import os
import json
import logging
import shutil
from datetime import datetime

from django.shortcuts import render, redirect
from django.http import HttpResponse, FileResponse
from django.conf import settings
from django.contrib import messages
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.forms import modelform_factory
from django.urls import reverse

from ..models import CompanyProfile
from ..forms import CompanyProfileForm
from .utils import is_htmx, htmx_response, redirect_to_staff
from ..decorators import handle_errors

logger = logging.getLogger(__name__)


# ==========================================================
# FIELD VALIDATION (HTMX)
# ==========================================================

def validate_setting_field(request):
    field_name = request.GET.get('field')
    if not field_name:
        return HttpResponse("Invalid field", status=400)

    value = request.GET.get(field_name, '')
    CompanyValidationForm = modelform_factory(CompanyProfile, form=CompanyProfileForm, fields=[field_name])

    try:
        form = CompanyValidationForm(data={field_name: value})
        html = render_to_string('settings/partials/field_errors.html', {'field': form[field_name]}, request=request)
        return HttpResponse(html)
    except Exception as e:
        logger.error(f"Validation error on {field_name}: {e}")
        return HttpResponse('<div class="invalid-feedback d-block">Server validation error</div>', status=500)


# ==========================================================
# COMPANY SETTINGS
# ==========================================================

@csrf_protect
@handle_errors(default_redirect='accounting:company_settings', htmx_template='settings/company_settings_form.html')
def company_settings(request):
    profile = CompanyProfile.get_instance()

    if request.method == 'POST':
        form = CompanyProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            form.save()
            logger.info(f"Company settings updated by {request.user.username}")

            if is_htmx(request):
                return htmx_response(
                    request,
                    'settings/company_settings_form.html',
                    context={'form': form, 'profile': profile},
                    toast={'level': 'success', 'message': 'Company settings updated successfully.'}
                )
            messages.success(request, "Company settings updated successfully.")
            return redirect_to_staff('company_settings')
        else:
            if is_htmx(request):
                return render(request, 'settings/company_settings_form.html', {'form': form, 'profile': profile})
    else:
        form = CompanyProfileForm(instance=profile)

    return render(request, 'settings/company_settings.html', {'form': form, 'profile': profile})


# ==========================================================
# BACKUP DATABASE
# ==========================================================

def backup_database(request):
    db_path = settings.DATABASES['default']['NAME']

    if not os.path.exists(db_path):
        messages.error(request, "Database file not found.")
        if is_htmx(request):
            return htmx_response(
                request,
                'settings/company_settings_form.html',
                context={'form': CompanyProfileForm(instance=CompanyProfile.get_instance())},
                toast={'level': 'danger', 'message': 'Database file not found.'}
            )
        return redirect_to_staff('company_settings')

    try:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"backup_{timestamp}.sqlite3"

        # For HTMX requests, we need to trigger a download via redirect
        if is_htmx(request):
            response = HttpResponse()
            response['HX-Redirect'] = reverse('accounting:backup_database') + '?download=1'
            response['HX-Trigger'] = json.dumps({
                'showToast': {'level': 'info', 'message': 'Preparing backup...'}
            })
            return response

        # Normal download
        response = FileResponse(open(db_path, 'rb'), content_type='application/octet-stream')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        logger.info(f"Database backup downloaded by {request.user.username}")
        return response
    except Exception as e:
        logger.error(f"Error creating database backup: {e}")
        messages.error(request, "Failed to create backup. Please try again.")
        if is_htmx(request):
            return htmx_response(
                request,
                'settings/company_settings_form.html',
                context={'form': CompanyProfileForm(instance=CompanyProfile.get_instance())},
                toast={'level': 'danger', 'message': 'Failed to create backup.'}
            )
        return redirect_to_staff('company_settings')


# ==========================================================
# RESTORE DATABASE (CSRF PROTECTED)
# ==========================================================

@csrf_protect
@require_http_methods(["GET", "POST"])
def restore_database(request):
    # GET request: show the restore form in modal
    if request.method == 'GET':
        return render(request, 'settings/restore.html')

    # POST request: handle file upload
    if request.method == 'POST':
        if not request.FILES.get('db_file'):
            if is_htmx(request):
                return htmx_response(
                    request,
                    'settings/restore.html',
                    toast={'level': 'danger', 'message': 'Please select a file.'}
                )
            messages.error(request, "Please select a database file to restore.")
            return render(request, 'settings/restore.html')

        db_file = request.FILES['db_file']
        db_path = settings.DATABASES['default']['NAME']

        # Validate file size (max 100MB)
        if db_file.size > 100 * 1024 * 1024:
            if is_htmx(request):
                return htmx_response(
                    request,
                    'settings/restore.html',
                    toast={'level': 'danger', 'message': 'File size too large (max 100MB).'}
                )
            messages.error(request, "File size too large. Maximum 100MB allowed.")
            return render(request, 'settings/restore.html')

        # Validate file extension
        if not db_file.name.endswith('.sqlite3') and not db_file.name.endswith('.db'):
            if is_htmx(request):
                return htmx_response(
                    request,
                    'settings/restore.html',
                    toast={'level': 'danger', 'message': 'Invalid file format. Please upload a .sqlite3 or .db file.'}
                )
            messages.error(request, "Invalid file format. Please upload a SQLite database file (.sqlite3 or .db).")
            return render(request, 'settings/restore.html')

        try:
            # Create backup of current database before restore
            if os.path.exists(db_path):
                backup_path = f"{db_path}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                shutil.copy2(db_path, backup_path)
                logger.info(f"Pre-restore backup created: {backup_path}")

            # Write the uploaded file
            with open(db_path, 'wb') as f:
                for chunk in db_file.chunks():
                    f.write(chunk)

            logger.info(f"Database restored by {request.user.username} from file: {db_file.name}")

            if is_htmx(request):
                response = HttpResponse()
                response['HX-Redirect'] = reverse('accounting:company_settings')
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'success',
                        'message': 'Database restored successfully. Please restart the application.'
                    },
                    'closeModal': ''
                })
                return response

            messages.success(request, "Database restored successfully. Please restart the application.")
            return redirect_to_staff('company_settings')

        except Exception as e:
            logger.error(f"Error restoring database: {e}")
            if is_htmx(request):
                return htmx_response(
                    request,
                    'settings/restore.html',
                    toast={'level': 'danger', 'message': f'Restore failed: {str(e)}'}
                )
            messages.error(request, f"Restore failed: {str(e)}")
            return render(request, 'settings/restore.html')

    # Any other method (should not happen)
    return HttpResponse("Method not allowed", status=405)



        