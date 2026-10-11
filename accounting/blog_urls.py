from django.urls import path
from .views import blog_views as views

app_name = 'blog'

urlpatterns = [
    # Public
    path('', views.post_list, name='post_list'),
    path('category/<slug:slug>/', views.category_posts, name='category'),
    path('tag/<slug:slug>/', views.tag_posts, name='tag'),
    path('<slug:slug>/', views.post_detail, name='post_detail'),

    # Staff (HTMX-powered CRUD)
    path('staff/manage/', views.manage_blog, name='manage_blog'),
    path('staff/add/', views.blog_post_create, name='post_create'),
    path('staff/<int:pk>/edit/', views.blog_post_edit, name='post_edit'),
    path('staff/<int:pk>/delete/', views.blog_post_delete, name='post_delete'),
]
