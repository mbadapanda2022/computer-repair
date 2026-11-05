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
from django.utils.module_loading import import_string
from django.views.decorators.http import require_GET

from ..models import CompanyProfile
from ..tools_registry import TOOL_SCRIPTS, TOOLS, get_tool, grouped_tools

SHELL_TEMPLATE = 'tools/index.html'
WELCOME_PARTIAL = 'tools/partials/_welcome.html'


def _company_name():
    return CompanyProfile.get_instance().name or 'My Company'


def shell_context(request, tool):
    """Context for the page shell, with per-tool SEO metadata when open."""
    company = _company_name()
    index_title = 'Tech Tools | ' + company
    if tool:
        title = f'{tool["seo_title"]} | {company}'
        description = tool['seo_description']
    else:
        title = index_title
        # Stays under 160 characters so search results show the whole line
        # instead of an ellipsis in the middle of the offer.
        description = ('Free bench calculators and an IC marking-code and '
                       'pinout finder for electronics and computer repair — '
                       f'resistors, fuses, subnets, GST. By {company}.')
    return {
        'tool_groups': grouped_tools(),
        'tool_count': len(TOOLS),
        'active_tool': tool['slug'] if tool else '',
        'initial_partial': tool['partial'] if tool else WELCOME_PARTIAL,
        'script_files': TOOL_SCRIPTS,
        'site_name': company,
        'index_title': index_title,
        'meta': {
            'title': title,
            'description': description,
            'og_url': request.build_absolute_uri(),
        },
    }


@require_GET
def tools_index(request):
    return render(request, SHELL_TEMPLATE, shell_context(request, None))


@require_GET
def tools_detail(request, slug):
    tool = get_tool(slug)
    if not tool:
        raise Http404('Unknown tech tool')

    # Most tools are pure browser maths: their pane is markup with no context,
    # so the same partial is served for a page load and an HTMX swap. A tool
    # that needs the server (a database-backed one, like the IC Finder) names
    # a view here, and that view owns both modes.
    pane_view = tool.get('pane_view')
    if pane_view:
        return import_string(pane_view)(request, tool=tool)

    # HTMX only wants the pane; the shell (and its SEO tags) is already there.
    # Tool panes are markup only — static/js/tools/* binds them by slug.
    if request.headers.get('HX-Request', '').lower() == 'true':
        return render(request, tool['partial'])

    return render(request, SHELL_TEMPLATE, shell_context(request, tool))
