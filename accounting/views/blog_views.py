from django.shortcuts import render, get_object_or_404, redirect
from django.core.paginator import Paginator
from django.db.models import Q
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.http import require_http_methods
from django.template.loader import render_to_string
from django.http import HttpResponse
import json

from ..models import BlogPost, BlogCategory, BlogTag
from ..forms import BlogPostFilterForm, BlogPostForm
from .utils import is_htmx


def _published_qs():
    return (
        BlogPost.objects.published()
        .select_related('author', 'category')
        .prefetch_related('tags')
    )


def _staff_qs():
    return (
        BlogPost.objects
        .select_related('author', 'category')
        .prefetch_related('tags')
        .order_by('-published_at', '-created_at')
    )


def _common_context(**extra):
    ctx = {
        'categories': BlogCategory.objects.all(),
        'tags': BlogTag.objects.all()[:20],
    }
    ctx.update(extra)
    return ctx


def _staff_page_obj(request):
    paginator = Paginator(_staff_qs(), 12)
    return paginator.get_page(request.GET.get('page'))


# ============================================================
# PUBLIC VIEWS
# ============================================================

def post_list(request):
    qs = _published_qs()
    form = BlogPostFilterForm(request.GET)

    search = request.GET.get('search')
    category_slug = request.GET.get('category')
    tag_slug = request.GET.get('tag')

    if search:
        qs = qs.filter(Q(title__icontains=search) | Q(excerpt__icontains=search))
    if category_slug:
        qs = qs.filter(category__slug=category_slug)
    if tag_slug:
        qs = qs.filter(tags__slug=tag_slug)

    paginator = Paginator(qs, 9)
    page = paginator.get_page(request.GET.get('page'))

    return render(request, 'blog/list.html', {
        'form': form,
        'page_obj': page,
        **_common_context(),
    })


def category_posts(request, slug):
    category = get_object_or_404(BlogCategory, slug=slug, is_active=True)
    qs = _published_qs().filter(category=category)
    paginator = Paginator(qs, 9)
    page = paginator.get_page(request.GET.get('page'))

    return render(request, 'blog/list.html', {
        'form': BlogPostFilterForm(),
        'page_obj': page,
        'category': category,
        **_common_context(),
    })


def tag_posts(request, slug):
    tag = get_object_or_404(BlogTag, slug=slug)
    qs = _published_qs().filter(tags=tag)
    paginator = Paginator(qs, 9)
    page = paginator.get_page(request.GET.get('page'))

    return render(request, 'blog/list.html', {
        'form': BlogPostFilterForm(),
        'page_obj': page,
        'tag': tag,
        **_common_context(),
    })


def post_detail(request, slug):
    post = get_object_or_404(_published_qs(), slug=slug)
    post.increment_views()

    related = (
        _published_qs().filter(category=post.category).exclude(pk=post.pk)[:3]
        if post.category_id else []
    )

    return render(request, 'blog/detail.html', {
        'post': post,
        'related': related,
        **_common_context(),
    })


# ============================================================
# STAFF VIEWS (HTMX-powered CRUD)
# ============================================================

@staff_member_required
def manage_blog(request):
    """Staff page to manage blog posts (list + Add/Edit/Delete)."""
    page_obj = _staff_page_obj(request)
    return render(request, 'blog/manage_blog.html', {
        'page_obj': page_obj,
        **_common_context(),
    })


@staff_member_required
def blog_post_create(request):
    """Create a new blog post via HTMX modal."""
    if request.method == 'POST':
        form = BlogPostForm(request.POST, request.FILES)
        if form.is_valid():
            post = form.save(commit=False)
            if not post.author_id:
                post.author = request.user
            post.save()
            form.save_m2m()
            return _success_response(request, 'Blog post "{}" created.'.format(post.title))
        if not is_htmx(request):
            messages.error(request, 'Please correct the errors below.')
            return redirect('blog:post_create')
    else:
        form = BlogPostForm()
    return render(request, 'blog/partials/_post_form.html', {
        'form': form, 'action': 'Add', **_common_context()
    })


@staff_member_required
def blog_post_edit(request, pk):
    """Edit an existing blog post via HTMX modal."""
    post = get_object_or_404(BlogPost, pk=pk)
    if request.method == 'POST':
        form = BlogPostForm(request.POST, request.FILES, instance=post)
        if form.is_valid():
            post = form.save(commit=False)
            if not post.author_id:
                post.author = request.user
            post.save()
            form.save_m2m()
            return _success_response(request, 'Blog post updated successfully.')
        if not is_htmx(request):
            messages.error(request, 'Please correct the errors below.')
            return redirect('blog:post_edit', pk=post.pk)
    else:
        form = BlogPostForm(instance=post)
    return render(request, 'blog/partials/_post_form.html', {
        'form': form, 'action': 'Edit', 'post': post, **_common_context()
    })


@staff_member_required
@require_http_methods(["DELETE"])
def blog_post_delete(request, pk):
    """Delete a blog post via HTMX."""
    post = get_object_or_404(BlogPost, pk=pk)
    title = post.title
    post.delete()
    return _success_response(request, 'Blog post "{}" deleted.'.format(title))


def _success_response(request, message):
    """Return HTMX response that refreshes the blog list, closes modal, and shows toast."""
    page_obj = _staff_page_obj(request)
    html = render_to_string('blog/staff/_post_list.html', {
        'page_obj': page_obj, **_common_context()
    }, request=request)
    response = HttpResponse(html)
    response['HX-Trigger'] = json.dumps({
        'showToast': {'level': 'success', 'message': message},
        'closeModal': '',
    })
    return response
