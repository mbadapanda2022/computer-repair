# accounting/tests_google_reviews.py
from unittest.mock import patch

from django.test import TestCase, override_settings

from .models import Testimonial
from .google_reviews import sync_google_reviews, is_configured

GOOGLE_REVIEWS = [
    {
        'reviewId': 'r1',
        'rating': 5,
        'text': {'text': 'Excellent chip-level repair!'},
        'authorAttribution': {'displayName': 'Rahul', 'photoUri': 'https://example.com/a.jpg'},
        'googleMapsUri': 'https://maps.google.com/r1',
        'publishTime': '2026-01-02T03:04:05Z',
    },
    {
        'reviewId': 'r2',
        'rating': 4,
        'text': {'text': 'Fast service, fair price.'},
        'authorAttribution': {'displayName': 'Sita'},
        'googleMapsUri': 'https://maps.google.com/r2',
    },
    {
        'reviewId': 'r3',
        'rating': 3,
        'text': {'text': ''},  # no text -> skipped
        'authorAttribution': {'displayName': 'Empty'},
    },
]


@override_settings(GOOGLE_PLACES_API_KEY='test-key', GOOGLE_PLACE_ID='ChIJ-test')
class GoogleReviewsSyncTest(TestCase):

    def test_is_configured(self):
        self.assertTrue(is_configured())

    @patch('accounting.google_reviews.fetch_google_reviews', return_value=GOOGLE_REVIEWS)
    def test_sync_creates_reviews(self, mock_fetch):
        stats = sync_google_reviews()
        self.assertIsNone(stats['error'])
        self.assertEqual(stats['created'], 2)
        self.assertEqual(stats['skipped'], 1)
        t = Testimonial.objects.get(google_review_id='r1')
        self.assertEqual(t.source, 'google')
        self.assertEqual(t.customer_name, 'Rahul')
        self.assertEqual(t.rating, 5)
        self.assertEqual(t.review_url, 'https://maps.google.com/r1')
        self.assertIsNotNone(t.reviewed_at)

    @patch('accounting.google_reviews.fetch_google_reviews', return_value=GOOGLE_REVIEWS)
    def test_second_sync_updates_and_deactivates_missing(self, mock_fetch):
        sync_google_reviews()
        changed = [dict(GOOGLE_REVIEWS[0])]
        changed[0]['text'] = {'text': 'Updated review text'}
        with patch('accounting.google_reviews.fetch_google_reviews', return_value=[changed[0]]):
            stats = sync_google_reviews()
        self.assertEqual(stats['updated'], 1)
        self.assertEqual(stats['deactivated'], 1)  # r2 vanished from Google's top list
        self.assertEqual(
            Testimonial.objects.get(google_review_id='r1').review_text,
            'Updated review text',
        )
        self.assertFalse(Testimonial.objects.get(google_review_id='r2').is_active)

    @patch('accounting.google_reviews.fetch_google_reviews', side_effect=Exception('quota exceeded'))
    def test_api_failure_keeps_existing_reviews(self, mock_fetch):
        Testimonial.objects.create(
            customer_name='Manual', review_text='keep me', rating=5, source='manual'
        )
        with patch('accounting.google_reviews.fetch_google_reviews', return_value=GOOGLE_REVIEWS):
            sync_google_reviews()
        stats = sync_google_reviews()
        self.assertIn('quota exceeded', stats['error'])
        self.assertEqual(Testimonial.objects.count(), 3)  # nothing added/removed

    @patch('accounting.google_reviews.fetch_google_reviews', return_value=GOOGLE_REVIEWS)
    def test_manual_reviews_never_deactivated(self, mock_fetch):
        m = Testimonial.objects.create(
            customer_name='Manual', review_text='hello', rating=5, source='manual'
        )
        sync_google_reviews()
        m.refresh_from_db()
        self.assertTrue(m.is_active)
        self.assertEqual(m.source, 'manual')

    @patch('accounting.google_reviews.fetch_google_reviews', return_value=GOOGLE_REVIEWS)
    def test_soft_deleted_review_not_resurrected(self, mock_fetch):
        sync_google_reviews()
        t = Testimonial.objects.get(google_review_id='r1')
        t.delete()  # soft delete
        stats = sync_google_reviews()
        self.assertEqual(stats['skipped'], 2)  # r1 (deleted) + r3 (empty text)
        self.assertFalse(Testimonial.all_objects.get(google_review_id='r1').is_deleted is False)


@override_settings(GOOGLE_PLACES_API_KEY='', GOOGLE_PLACE_ID='')
class GoogleReviewsUnconfiguredTest(TestCase):

    def test_sync_noop_without_credentials(self):
        stats = sync_google_reviews()
        self.assertIsNotNone(stats['error'])
        self.assertEqual(Testimonial.objects.count(), 0)
