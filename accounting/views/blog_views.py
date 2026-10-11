from django.shortcuts import render, get_object_or_404, redirect
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Q
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.http import require_http_methods
from django.template.loader import render_to_string
from django.http import HttpResponse
from django.utils import timezone
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

def _blog_meta(title=None, description=None):
    """Build a meta dict for blog pages (SEO)."""
    return {
        'title': title or 'Blog',
        'description': description or 'Insights, tips, and the latest from our repair experts.',
        'keywords': 'computer repair, laptop repair, tech tips, DIY guides',
        'og_url': '',
        'og_image': '',
    }


def _blog_page_context(request, queryset, page_size=9, **extra):
    """Paginate a queryset and return context dict for blog list partials."""
    paginator = Paginator(queryset, page_size)
    page = paginator.get_page(request.GET.get('page'))
    ctx = {
        'page_obj': page,
        'paginator': paginator,
    }
    ctx.update(extra)
    ctx.update(_common_context())
    return ctx


def _blog_list_context(request):
    """Build common context for the blog list page (categories, tags, filter form)."""
    return {
        'form': BlogPostFilterForm(request.GET),
        **_common_context(),
    }


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

    ctx = _blog_page_context(request, qs)
    if is_htmx(request):
        return render(request, 'blog/partials/_blog_list.html', ctx)

    ctx.update(_blog_list_context(request))
    ctx['meta'] = _blog_meta(
        title='Blog',
        description='Tech tips, repair guides, and the latest from our repair experts.',
    )
    return render(request, 'blog/list.html', ctx)


def category_posts(request, slug):
    category = get_object_or_404(BlogCategory, slug=slug, is_active=True)
    qs = _published_qs().filter(category=category)

    ctx = _blog_page_context(request, qs, category=category)
    if is_htmx(request):
        return render(request, 'blog/partials/_blog_list.html', ctx)

    ctx.update(_blog_list_context(request))
    ctx['meta'] = _blog_meta(
        title=f'{category.name} – Blog',
        description=f'{category.name} articles and insights from our repair experts.',
    )
    return render(request, 'blog/list.html', ctx)


def tag_posts(request, slug):
    tag = get_object_or_404(BlogTag, slug=slug)
    qs = _published_qs().filter(tags=tag)

    ctx = _blog_page_context(request, qs, tag=tag)
    if is_htmx(request):
        return render(request, 'blog/partials/_blog_list.html', ctx)

    ctx.update(_blog_list_context(request))
    ctx['meta'] = _blog_meta(
        title=f'#{tag.name} – Blog',
        description=f'Posts tagged #{tag.name} from our repair experts.',
    )
    return render(request, 'blog/list.html', ctx)


def post_detail(request, slug):
    post = get_object_or_404(_published_qs(), slug=slug)
    post.increment_views()

    related = (
        _published_qs().filter(category=post.category).exclude(pk=post.pk)[:3]
        if post.category_id else []
    )

    meta = _blog_meta(
        title=post.effective_meta_title,
        description=post.effective_meta_description or post.excerpt,
    )
    if post.cover_image:
        meta['og_image'] = post.cover_image.url
    if post.canonical_url:
        meta['og_url'] = post.canonical_url

    return render(request, 'blog/detail.html', {
        'post': post,
        'related': related,
        'meta': meta,
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
        if is_htmx(request):
            resp = render(request, 'blog/partials/_post_form.html', {
                'form': form, 'action': 'Add', **_common_context()
            })
            resp['HX-Retarget'] = '#mainModalContent'
            return resp
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
        if is_htmx(request):
            resp = render(request, 'blog/partials/_post_form.html', {
                'form': form, 'action': 'Edit', 'post': post, **_common_context()
            })
            resp['HX-Retarget'] = '#mainModalContent'
            return resp
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
