"""
Bank-book guards: transactions generated from a Payment must be edited or
deleted through that payment, otherwise the bank book silently diverges
from the ledger.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounting.models import BankAccount, BankTransaction, Contact, Payment

User = get_user_model()


@override_settings(
    ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
    SECURE_SSL_REDIRECT=False,
)
class BankTransactionGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='bank_guard_staff', password='TestPass123!',
            is_staff=True, is_active=True,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)
        self.account = BankAccount.objects.create(
            name='Guard Bank', bank_name='Test Bank',
            opening_balance=Decimal('0.00'),
        )
        self.contact = Contact.objects.create(
            contact_type='customer', name='Bank Guard Customer',
            phone='9876543210',
        )
        # Payment.save() mirrors itself into the bank book.
        self.payment = Payment.objects.create(
            direction='received', contact=self.contact,
            amount=Decimal('500.00'), method='bank',
            bank_account=self.account, date=timezone.localdate(),
        )
        self.txn = BankTransaction.objects.get(payment=self.payment)

    def test_payment_mirror_cannot_be_edited_from_bank_book(self):
        response = self.client.post(
            reverse('accounting:bank_transaction_edit', args=[self.txn.pk]),
            {
                'transaction_type': 'deposit',
                'source_type': 'payment_received',
                'amount': '999.00',
                'date': timezone.localdate().isoformat(),
                'description': 'tampered',
                'reference': '',
            },
        )
        self.assertEqual(response.status_code, 400)
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.amount, Decimal('500.00'))
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.amount, Decimal('500.00'))

    def test_payment_mirror_cannot_be_deleted_from_bank_book(self):
        response = self.client.delete(
            reverse('accounting:bank_transaction_delete', args=[self.txn.pk]),
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(BankTransaction.objects.filter(pk=self.txn.pk).exists())
        self.payment.refresh_from_db()
        self.assertFalse(self.payment.is_deleted)

    def test_manual_transaction_still_editable(self):
        manual = BankTransaction.objects.create(
            bank_account=self.account, transaction_type='deposit',
            source_type='manual', amount=Decimal('100.00'),
            date=timezone.localdate(), description='Cash deposit',
        )
        response = self.client.post(
            reverse('accounting:bank_transaction_edit', args=[manual.pk]),
            {
                'transaction_type': 'deposit',
                'source_type': 'manual',
                'amount': '125.00',
                'date': timezone.localdate().isoformat(),
                'description': 'Cash deposit',
                'reference': '',
            },
        )
        self.assertIn(response.status_code, (200, 302))
        manual.refresh_from_db()
        self.assertEqual(manual.amount, Decimal('125.00'))
