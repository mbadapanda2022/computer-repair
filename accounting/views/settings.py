# accounting/views/settings.py

import os
import json
import logging
import tempfile
from datetime import datetime
from io import StringIO
from django.shortcuts import render
from django.core.management import call_command
from django.http import HttpResponse, FileResponse
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


# =============================================================
# FIELD VALIDATION (HTMX)
# =============================================================
def validate_setting_field(request):
    field_name = request.GET.get('field')
    if not field_name:
        return HttpResponse("Invalid field", status=400)

    # File fields – GET validation skip
    if field_name in ['logo', 'hero_image', 'og_image']:
        return HttpResponse("") 

    value = request.GET.get(field_name, '')
    CompanyValidationForm = modelform_factory(
        CompanyProfile,
        form=CompanyProfileForm,
        fields=[field_name]
    )

    try:
        form = CompanyValidationForm(data={field_name: value})
        html = render_to_string(
            'settings/partials/field_errors.html',
            {'field': form[field_name]},
            request=request
        )
        return HttpResponse(html)
    except Exception as e:
        logger.error(f"Validation error on {field_name}: {e}")
        return HttpResponse(
            '<div class="invalid-feedback d-block">Server validation error</div>',
            status=500
        )


# =============================================================
# COMPANY SETTINGS – WITH CONSISTENT ERROR HANDLING
# =============================================================
@csrf_protect
@handle_errors(
    default_redirect='accounting:company_settings',
    htmx_template='settings/company_settings_form.html'
)
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
                return render(request, 'settings/company_settings_form.html', {
                    'form': form,
                    'profile': profile
                })
    else:
        form = CompanyProfileForm(instance=profile)

    return render(request, 'settings/company_settings.html', {
        'form': form,
        'profile': profile
    })


# =============================================================
# BACKUP DATABASE (SQLite + PostgreSQL compatible)
# =============================================================
@require_http_methods(["GET"])
def backup_database(request):
    try:
        # Create a JSON dump of the entire database
        output = StringIO()
        call_command('dumpdata', stdout=output, indent=2, exclude=['contenttypes', 'auth.permission'])
        
        # Get the dump content
        dump_content = output.getvalue()
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"backup_{timestamp}.json"
        
        # Create response with JSON file
        response = HttpResponse(
            dump_content,
            content_type='application/json'
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        logger.info(f"Database backup created by {request.user.username} (Size: {len(dump_content)} bytes)")
        return response
        
    except Exception as e:
        logger.error(f"Error creating database backup: {e}")
        if is_htmx(request):
            return htmx_response(
                request,
                'settings/company_settings_form.html',
                context={'form': CompanyProfileForm(instance=CompanyProfile.get_instance())},
                toast={'level': 'danger', 'message': f'Backup failed: {str(e)}'}
            )
        messages.error(request, f"Backup failed: {str(e)}")
        return redirect_to_staff('company_settings')


# =============================================================
# RESTORE DATABASE (SQLite + PostgreSQL compatible)
# =============================================================
@csrf_protect
@require_http_methods(["GET", "POST"])
def restore_database(request):
    # GET: show restore form in modal
    if request.method == 'GET':
        return render(request, 'settings/restore.html')

    # POST: handle file upload and restore
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

        # Validate file extension (JSON is the standard now)
        if not db_file.name.endswith('.json'):
            if is_htmx(request):
                return htmx_response(
                    request,
                    'settings/restore.html',
                    toast={'level': 'danger', 'message': 'Invalid file format. Please upload a .json backup file.'}
                )
            messages.error(request, "Invalid file format. Please upload a JSON backup file.")
            return render(request, 'settings/restore.html')

        try:
            # Save uploaded file to a temporary location
            with tempfile.NamedTemporaryFile(mode='wb+', suffix='.json', delete=False) as tmp_file:
                for chunk in db_file.chunks():
                    tmp_file.write(chunk)
                tmp_path = tmp_file.name

            # Load the data using Django's loaddata command
            # We need to use the file path (not the file object)
            call_command('loaddata', tmp_path, verbosity=0)

            # Clean up temporary file
            os.unlink(tmp_path)

            logger.info(f"Database restored by {request.user.username} from file: {db_file.name}")

            if is_htmx(request):
                response = HttpResponse()
                response['HX-Redirect'] = reverse('accounting:company_settings')
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'success',
                        'message': 'Database restored successfully!'
                    },
                    'closeModal': ''
                })
                return response

            messages.success(request, "Database restored successfully!")
            return redirect_to_staff('company_settings')

        except Exception as e:
            logger.error(f"Error restoring database: {e}")
            # Clean up temporary file if it exists
            if 'tmp_path' in locals():
                try:
                    os.unlink(tmp_path)
                except:
                    pass

            if is_htmx(request):
                return htmx_response(
                    request,
                    'settings/restore.html',
                    toast={'level': 'danger', 'message': f'Restore failed: {str(e)}'}
                )
            messages.error(request, f"Restore failed: {str(e)}")
            return render(request, 'settings/restore.html')

    return HttpResponse("Method not allowed", status=405)