"""
Public "Tech Tools" pages — free engineering / shop calculators.

One shell template (sidebar + content pane) is rendered for every URL:

* ``/tools/``                 → shell with the welcome panel
* ``/tools/<slug>/`` full GET → same shell, with that tool already open
  (so a shared link, a refresh and the Back button all work standalone)
* ``/tools/<slug>/`` HX-GET   → only the tool's own markup, which HTMX swaps
  into the content pane without reloading the page

Everything is public and needs no login; the sidebar contents and the valid
slugs come from ``accounting.tools_registry``, so a URL that is not in the
registry 404s instead of rendering an empty pane.
"""
from __future__ import annotations

from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_GET

from ..models import CompanyProfile
from ..tools_registry import TOOL_SCRIPTS, TOOLS, get_tool, grouped_tools

SHELL_TEMPLATE = 'tools/index.html'
WELCOME_PARTIAL = 'tools/partials/_welcome.html'


def _company_name():
    return CompanyProfile.get_instance().name or 'My Company'


def _shell(request, tool):
    """Context for the page shell, with per-tool SEO metadata when open."""
    company = _company_name()
    if tool:
        title = f'{tool["seo_title"]} | {company}'
        description = tool['seo_description']
    else:
        title = f'Tech Tools | {company}'
        description = ('Free calculators for repair work — resistor and '
                       'capacitor codes, Ohm\'s law, LED resistors, battery '
                       f'runtime, subnets and GST. Tools by {company}.')
    return {
        'tool_groups': grouped_tools(),
        'tool_count': len(TOOLS),
        'active_tool': tool['slug'] if tool else '',
        'initial_partial': tool['partial'] if tool else WELCOME_PARTIAL,
        'script_files': TOOL_SCRIPTS,
        'site_name': company,
        'meta': {
            'title': title,
            'description': description,
            'og_url': request.build_absolute_uri(),
        },
    }


@require_GET
def tools_index(request):
    return render(request, SHELL_TEMPLATE, _shell(request, None))


@require_GET
def tools_detail(request, slug):
    tool = get_tool(slug)
    if not tool:
        raise Http404('Unknown tech tool')

    # HTMX only wants the pane; the shell (and its SEO tags) is already there.
    # Tool panes are markup only — static/js/tools/* binds them by slug.
    if request.headers.get('HX-Request', '').lower() == 'true':
        return render(request, tool['partial'])

    return render(request, SHELL_TEMPLATE, _shell(request, tool))
