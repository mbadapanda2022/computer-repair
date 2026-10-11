from django.shortcuts import render, get_object_or_404
from django.core.paginator import Paginator
from django.db.models import Q

from ..models import BlogPost, BlogCategory, BlogTag
from ..forms import BlogPostFilterForm


def _published_qs():
    return (
        BlogPost.objects.published()
        .select_related('author', 'category')
        .prefetch_related('tags')
    )


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
        'categories': BlogCategory.objects.filter(is_active=True),
        'tags': BlogTag.objects.all()[:20],
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
        'categories': BlogCategory.objects.filter(is_active=True),
        'tags': BlogTag.objects.all()[:20],
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
        'categories': BlogCategory.objects.filter(is_active=True),
        'tags': BlogTag.objects.all()[:20],
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
        'categories': BlogCategory.objects.filter(is_active=True),
        'tags': BlogTag.objects.all()[:20],
    })
