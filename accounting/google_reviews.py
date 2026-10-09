# accounting/google_reviews.py
"""
Google Business Profile reviews → Testimonial sync (official Places API).

Uses the Google Places API (New) `places.get` endpoint with the `reviews`
field mask. Since March 2025 Google only returns reviews for a Business
Profile you own/are permitted on, and only the ~5 most relevant ones.

Design rules:
- Manual testimonials are NEVER touched by the sync.
- On any API failure we log and return stats with `error` set; previously
  synced Google reviews stay visible (fallback requirement).
- Reviews that disappear from Google's response are deactivated so the
  site never shows stale Google content (ToS: data must be refreshed).
"""
import logging
import threading
from datetime import datetime, timezone

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import connections

from .models import Testimonial

logger = logging.getLogger(__name__)

PLACES_ENDPOINT = "https://places.googleapis.com/v1/places/{place_id}"
# Google ToS: request only the fields you use.
FIELD_MASK = "reviews"
REQUEST_TIMEOUT = 10  # seconds

_SYNC_LOCK_KEY = 'google_reviews:sync_lock'

# Cache lock TTL: interval + buffer so only one worker/process syncs.
_LOCK_BUFFER = 300


def is_configured():
    return bool(settings.GOOGLE_PLACES_API_KEY and settings.GOOGLE_PLACE_ID)


def fetch_google_reviews():
    """Return the list of review dicts from Google, or raise on failure."""
    url = PLACES_ENDPOINT.format(place_id=settings.GOOGLE_PLACE_ID)
    resp = requests.get(
        url,
        headers={
            'X-Goog-Api-Key': settings.GOOGLE_PLACES_API_KEY,
            'X-Goog-FieldMask': FIELD_MASK,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get('reviews', [])


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _review_to_fields(review):
    """Map a Places API review to Testimonial field values (None if unusable)."""
    review_id = review.get('reviewId')
    text = ((review.get('text') or {}).get('text') or '').strip()
    rating = review.get('rating')
    if not review_id or not text or not rating:
        return None
    author = (review.get('authorAttribution') or {})
    return {
        'customer_name': (author.get('displayName') or 'Google Customer')[:100],
        'review_text': text,
        'rating': min(int(rating), 5),
        'author_photo_url': (author.get('photoUri') or '')[:500],
        'review_url': (review.get('googleMapsUri') or '')[:500],
        'reviewed_at': _parse_dt(review.get('publishTime') or review.get('createTime')),
    }


def sync_google_reviews(dry_run=False):
    """
    Upsert Google reviews into Testimonial (source='google').
    Returns a stats dict; never raises.
    """
    stats = {
        'fetched': 0, 'created': 0, 'updated': 0,
        'deactivated': 0, 'skipped': 0, 'error': None,
    }
    if not is_configured():
        stats['error'] = 'Google Places API not configured (GOOGLE_PLACES_API_KEY / GOOGLE_PLACE_ID missing)'
        return stats

    try:
        reviews = fetch_google_reviews()
    except Exception as e:
        logger.error("Google reviews fetch failed: %s", e)
        stats['error'] = str(e)
        return stats

    stats['fetched'] = len(reviews)
    seen_ids = []

    for review in reviews:
        fields = _review_to_fields(review)
        if fields is None:
            stats['skipped'] += 1
            continue
        review_id = review['reviewId']
        seen_ids.append(review_id)

        existing = Testimonial.all_objects.filter(google_review_id=review_id).first()
        if existing is None:
            stats['created'] += 1
            if not dry_run:
                Testimonial.all_objects.create(
                    google_review_id=review_id,
                    source='google',
                    is_active=True,
                    **fields,
                )
        elif existing.is_deleted:
            # Staff deleted this review deliberately — do not resurrect.
            stats['skipped'] += 1
        else:
            stats['updated'] += 1
            if not dry_run:
                for key, value in fields.items():
                    setattr(existing, key, value)
                existing.source = 'google'
                existing.is_active = True
                existing.save()

    if seen_ids and not dry_run:
        stale = Testimonial.objects.filter(
            source='google', is_active=True
        ).exclude(google_review_id__in=seen_ids)
        stats['deactivated'] = stale.update(is_active=False)

    if not stats['error']:
        logger.info("Google reviews sync: %s", stats)
    return stats


def _sync_in_thread():
    try:
        connections.close_all()
        stats = sync_google_reviews()
        if stats['error']:
            # Release the lock early so the next page view can retry.
            cache.delete(_SYNC_LOCK_KEY)
    except Exception:
        logger.exception("Background Google reviews sync crashed")
        cache.delete(_SYNC_LOCK_KEY)
    finally:
        connections.close_all()


def maybe_background_refresh():
    """
    Fire-and-forget sync at most once per GOOGLE_REVIEWS_SYNC_INTERVAL.
    Safe to call on every landing page hit; the cache lock makes it a
    no-op for concurrent requests and other gunicorn workers.
    """
    if not is_configured():
        return
    acquired = cache.add(
        _SYNC_LOCK_KEY, '1',
        settings.GOOGLE_REVIEWS_SYNC_INTERVAL + _LOCK_BUFFER,
    )
    if acquired:
        threading.Thread(target=_sync_in_thread, daemon=True).start()
