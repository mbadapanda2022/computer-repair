"""
Seed a realistic but fully GENERIC demo dataset for DEMO_MODE packages.

NEVER contains real business data — every name, phone, email, GSTIN,
address and amount here is invented fixture data. Guard refuses to run
outside DEMO_MODE so it can never touch a production database.

Usage (demo launcher does this automatically on first run):
    DEMO_MODE=true python manage.py demo_seed [--fresh]
"""
import random
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounting.models import (
    BankAccount,
    CompanyProfile,
    Contact,
    ContactMessage,
    CreditNote,
    CreditNoteItem,
    FAQ,
    Invoice,
    InvoiceItem,
    Notification,
    Payment,
    PaymentAllocation,
    Product,
    ProductCategory,
    Purchase,
    PurchaseAllocation,
    PurchaseItem,
    RepairJob,
    RepairPart,
    RepairService,
    Service,
    Testimonial,
    sync_purchase_ledger,
)

COMPANY = {
    'name': 'Demo Computer Care',
    'address': '123 Sample Street, sahi at Sahi Nagar, Bhubaneswar, Odisha 751001',
    'phone': '9800000001',
    'email': 'support@demo-computer-care.invalid',
    'gstin': '21ABCDE1234F1Z5',
    'state': 'Odisha',
    'city': 'Bhubaneswar',
    'website_url': 'http://127.0.0.1:8777',
    'tagline': 'Expert chip-level repair, sales, and service for all brands.',
    'invoice_prefix': 'INV',
    'default_tax_rate': Decimal('18.00'),
    'financial_year_start': timezone.localdate().replace(year=timezone.localdate().year - 1, month=4, day=1),
}

CUSTOMERS = [
    # name, phone, email, city, state, opening_balance
    ('Ravi Kumar', '9800000011', 'ravi.kumar@example.com', 'Bhubaneswar', 'Odisha', Decimal('0')),
    ('Sita Sharma', '9800000012', 'sita.sharma@example.com', 'Cuttack', 'Odisha', Decimal('0')),
    ('Amit Patel', '9800000013', 'amit.patel@example.com', 'Bhubaneswar', 'Odisha', Decimal('0')),
    ('Neha Gupta', '9800000014', 'neha.gupta@example.com', 'Kharagpur', 'West Bengal', Decimal('0')),
    ('Vikram Singh', '9800000015', 'vikram.singh@example.com', 'Ranchi', 'Jharkhand', Decimal('0')),
]

VENDORS = [
    ('Sample Tech Distributors', '9800000021', 'sales@sampletechdist.example', 'Bhubaneswar', 'Odisha', '21ABCDE1234F1Z6'),
    ('City Peripherals Wholesale', '9800000022', 'info@cityperipherals.example', 'Kolkata', 'West Bengal', '19ABCDE1234F1Z7'),
]

PRODUCTS = [
    # name, hsn, category, unit, purchase, selling, stock, threshold, tax, is_service
    ('SSD 480GB SATA', '8471', 'Storage', 'pcs', '2400.00', '3200.00', 18, 5, '18.00', False),
    ('RAM 8GB DDR4', '8473', 'Memory', 'pcs', '1350.00', '1850.00', 26, 8, '18.00', False),
    ('Laptop Charger 65W Universal', '8504', 'Power', 'pcs', '700.00', '1100.00', 4, 5, '18.00', False),
    ('Keyboard USB Standard', '8443', 'Input', 'pcs', '380.00', '650.00', 35, 10, '18.00', False),
    ('Mouse Optical 1200DPI', '8443', 'Input', 'pcs', '180.00', '350.00', 42, 10, '18.00', False),
    ('Monitor 21.5 inch LED', '8528', 'Display', 'pcs', '6200.00', '7900.00', 7, 3, '18.00', False),
    ('WiFi Adapter USB', '8517', 'Networking', 'pcs', '320.00', '550.00', 30, 10, '18.00', False),
    ('Laptop Full Body Cleaning', '9987', 'Services', 'job', '0.00', '500.00', 0, 0, '18.00', True),
    ('Chip Level Repair Labour', '9987', 'Services', 'job', '0.00', '800.00', 0, 0, '18.00', True),
    ('OS Installation + Drivers', '9987', 'Services', 'job', '0.00', '700.00', 0, 0, '18.00', True),
]

DEMO_PASSWORD = 'Demo@12345'


class Command(BaseCommand):
    help = 'Seed generic demo data (only runs when DEMO_MODE=true).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--fresh', action='store_true',
            help='Delete existing demo business rows and re-seed from scratch.',
        )

    def handle(self, *args, **options):
        if not getattr(settings, 'DEMO_MODE', False):
            self.stderr.write(self.style.ERROR(
                'demo_seed refuses to run unless DEMO_MODE=true. '
                'This protects real business data from demo fixtures.'
            ))
            return 1
        if options['fresh']:
            self._wipe()
        elif Invoice.objects.exists() or RepairJob.objects.exists():
            self.stdout.write(self.style.WARNING(
                'Demo data already seeded — skipping (use --fresh to rebuild from scratch).'
            ))
            return 0
        with transaction.atomic():
            company = self._company()
            users = self._users()
            customers, vendors = self._contacts()
            products = self._products(company)
            banks = self._banks()
            self._purchases(vendors, products)
            invoices = self._invoices(customers, products, banks)
            self._repairs(customers, products, banks, users['admin'], invoices)
            self._credit_note(customers, products)
            self._expenses(banks, vendors)
            self._landing_content()
            self._messages_and_notifications(users['admin'])
        self.stdout.write(self.style.SUCCESS(
            'Demo data seeded. Login: Admin / %s (staff) and demo_customer / %s (customer portal).'
            % (DEMO_PASSWORD, DEMO_PASSWORD)
        ))
        return 0

    # ── helpers ────────────────────────────────────────────────
    def _wipe(self):
        from accounting.models import (
            AdvanceAdjustment,
            LedgerEntry,
            LedgerLine,
            PurchaseAllocation,
            RepairStatusLog,
            StockMovement,
        )
        for model in (Notification, ContactMessage, FAQ, Testimonial, Service,
                      RepairService, RepairPart, RepairStatusLog, RepairJob,
                      CreditNoteItem, CreditNote,
                      PaymentAllocation, PurchaseAllocation, AdvanceAdjustment,
                      Payment,
                      InvoiceItem, Invoice, PurchaseItem, Purchase,
                      LedgerLine, LedgerEntry, StockMovement,
                      BankAccount, ProductCategory, Product, Contact):
            manager = getattr(model, 'all_objects', model.objects)
            manager.all().delete()   # hard delete (bypasses soft-delete)

    def _company(self):
        company = CompanyProfile.get_instance()
        for field, value in COMPANY.items():
            setattr(company, field, value)
        labour = Product.objects.filter(name='Chip Level Repair Labour').first()
        if labour:
            company.default_labour_product = labour
        company.save()
        return company

    def _users(self):
        User = get_user_model()
        admin, _ = User.objects.get_or_create(
            username='Admin',
            defaults={'is_staff': True, 'is_superuser': True,
                      'email': 'admin@demo-computer-care.invalid',
                      'first_name': 'Demo', 'last_name': 'Admin'},
        )
        admin.set_password(DEMO_PASSWORD)
        admin.is_staff = True
        admin.is_superuser = True
        admin.save()
        customer_user, _ = User.objects.get_or_create(
            username='demo_customer',
            defaults={'email': 'ravi.kumar@example.com',
                      'first_name': 'Ravi', 'last_name': 'Kumar'},
        )
        customer_user.set_password(DEMO_PASSWORD)
        customer_user.save()
        return {'admin': admin, 'customer': customer_user}

    def _contacts(self):
        users = self._users()
        customers = []
        for i, (name, phone, email, city, state, opening) in enumerate(CUSTOMERS):
            c, _ = Contact.objects.get_or_create(
                phone=phone,
                defaults={
                    'contact_type': 'customer', 'name': name, 'email': email,
                    'address': f'{101 + i} Demo Colony, {city}',
                    'state': state, 'opening_balance': opening,
                },
            )
            if i == 0:
                c.user = users['customer']
                c.contact_type = 'both'
                c.save(update_fields=['user', 'contact_type'])
            customers.append(c)
        vendors = []
        for name, phone, email, city, state, gstin in VENDORS:
            v, _ = Contact.objects.get_or_create(
                phone=phone,
                defaults={
                    'contact_type': 'vendor', 'name': name, 'email': email,
                    'address': f'Wholesale Market, {city}', 'state': state,
                    'gstin': gstin,
                },
            )
            vendors.append(v)
        return customers, vendors

    def _products(self, company):
        products = {}
        for (name, hsn, category, unit, pp, sp, stock, low, tax, svc) in PRODUCTS:
            cat, _ = ProductCategory.objects.get_or_create(name=category)
            p, _ = Product.objects.get_or_create(
                name=name,
                defaults={
                    'hsn_code': hsn, 'category': cat, 'unit': unit,
                    'purchase_price': Decimal(pp), 'selling_price': Decimal(sp),
                    'current_stock': stock, 'low_stock_threshold': low,
                    'tax_rate': Decimal(tax), 'is_service': svc,
                },
            )
            products[name] = p
        if products.get('Chip Level Repair Labour'):
            company.default_labour_product = products['Chip Level Repair Labour']
            company.save(update_fields=['default_labour_product'])
        return products

    def _banks(self):
        hdfc, _ = BankAccount.objects.get_or_create(
            name='Demo Current A/c',
            defaults={'account_number': '000012345678', 'bank_name': 'Demo Bank',
                      'ifsc_code': 'DEMO0000123', 'account_type': 'current',
                      'opening_balance': Decimal('50000.00')},
        )
        cash, _ = BankAccount.objects.get_or_create(
            name='Shop Cash',
            defaults={'account_number': '', 'bank_name': '', 'ifsc_code': '',
                      'account_type': 'savings', 'opening_balance': Decimal('20000.00')},
        )
        return [hdfc, cash]

    def _d(self, days_ago):
        return timezone.localdate() - timezone.timedelta(days=days_ago)

    # ── purchases ──────────────────────────────────────────────
    def _purchases(self, vendors, products):
        plan = [
            (vendors[0], 40, [('SSD 480GB SATA', 10, '2400.00'),
                              ('RAM 8GB DDR4', 12, '1350.00')], 'intrastate', False),
            (vendors[1], 25, [('Mouse Optical 1200DPI', 20, '180.00'),
                              ('Keyboard USB Standard', 15, '380.00')], 'interstate', False),
            (vendors[0], 10, [('Monitor 21.5 inch LED', 5, '6200.00'),
                              ('WiFi Adapter USB', 10, '320.00')], 'intrastate', False),
        ]
        for vendor, days_ago, lines, gst, paid in plan:
            purchase = Purchase.objects.create(
                vendor=vendor, date=self._d(days_ago), gst_type=gst,
                notes='Demo purchase bill',
            )
            for pname, qty, price in lines:
                PurchaseItem.objects.create(
                    purchase=purchase, product=products[pname],
                    description=pname, quantity=Decimal(qty),
                    unit_price=Decimal(price),
                )
            purchase.refresh_from_db()
            sync_purchase_ledger(purchase)
            if paid:
                purchase.refresh_from_db()

    # ── sales invoices ─────────────────────────────────────────
    def _invoices(self, customers, products, banks):
        # (customer_idx, days_ago, gst_type, lines, pay: 'full'|'half'|None, bank_idx)
        plan = [
            (1, 35, 'intrastate', [('SSD 480GB SATA', 2, '3400.00'),
                                   ('RAM 8GB DDR4', 2, '1900.00')], 'full', 0),
            (2, 28, 'intrastate', [('Laptop Full Body Cleaning', 1, '500.00'),
                                   ('OS Installation + Drivers', 1, '700.00')], 'full', 1),
            (3, 20, 'interstate', [('Monitor 21.5 inch LED', 1, '8200.00'),
                                   ('Mouse Optical 1200DPI', 2, '390.00')], 'half', 0),
            (4, 15, 'intrastate', [('Keyboard USB Standard', 3, '690.00'),
                                   ('WiFi Adapter USB', 2, '599.00')], None, 0),
            (0, 10, 'intrastate', [('SSD 480GB SATA', 1, '3300.00'),
                                   ('Chip Level Repair Labour', 1, '900.00')], 'full', 1),
            (1, 5, 'intrastate', [('RAM 8GB DDR4', 1, '1899.00')], None, 0),
        ]
        invoices = []
        for (ci, days_ago, gst, lines, pay, bank_idx) in plan:
            customer = customers[ci]
            inv = Invoice.objects.create(
                customer=customer, date=self._d(days_ago), gst_type=gst,
                due_date=self._d(days_ago - 15), notes='Demo sales invoice',
            )
            for pname, qty, price in lines:
                InvoiceItem.objects.create(
                    invoice=inv, product=products[pname], description=pname,
                    quantity=Decimal(qty), unit_price=Decimal(price),
                    tax_rate=Decimal('18.00'),
                )
            inv.refresh_from_db()
            if pay:
                amount = inv.grand_total if pay == 'full' else (inv.grand_total / 2).quantize(Decimal('0.01'))
                payment = Payment.objects.create(
                    direction='received', contact=customer, amount=amount,
                    date=self._d(max(days_ago - 2, 0)),
                    method='bank' if bank_idx == 0 else 'cash',
                    bank_account=banks[bank_idx],
                    description='Demo payment',
                )
                PaymentAllocation.objects.create(payment=payment, invoice=inv, amount=amount)
                inv.refresh_from_db()
                inv.recompute_payment_state()
                inv.save(update_fields=['balance_due', 'payment_status'])
            invoices.append(inv)
        return invoices

    # ── repair jobs (full workflow) ────────────────────────────
    def _repairs(self, customers, products, banks, admin, invoices):
        labour = products['Chip Level Repair Labour']
        cleaning = products['Laptop Full Body Cleaning']

        def new_job(customer, device, issue, days_ago, **extra):
            job = RepairJob(
                customer=customer, device_model=device,
                serial_number=f'DEMOSN{random.randint(100000, 999999)}',
                issue_description=issue,
                received_by='Demo Staff',
                received_at=self._d(days_ago), date_in=self._d(days_ago),
                **extra,
            )
            job.save()
            return job

        # 1) Simple received job
        j1 = new_job(customers[0], 'Demo Laptop Model A15',
                     'Laptop not switching on. Customer suspects battery issue.', 3)

        # 2) Under diagnosis with pending estimate
        j2 = new_job(customers[1], 'Demo Desktop PC Tower',
                     'PC restarts randomly under load.', 6,
                     diagnosis_report='Suspected PSU + thermal paste issue; testing.',
                     estimated_cost=Decimal('2800.00'))

        # 3) Under repair, estimate approved
        j3 = new_job(customers[2], 'Demo Ultrabook U-14',
                     'Screen flicker and hinge loose.', 9,
                     estimated_cost=Decimal('4500.00'))
        j3.estimate_status = 'approved'
        j3.estimate_approved_at = timezone.now()
        j3.estimate_approved_by = admin
        j3.save()
        j1.change_status('received')
        j1.change_status('diagnosis')
        j2.change_status('received')
        j2.change_status('diagnosis')
        j3.change_status('received')
        j3.change_status('diagnosis')
        j3.change_status('repairing')

        # 4) Ready — parts + labour added, ready for delivery
        j4 = new_job(customers[3], 'Demo Printer LP-100',
                     'Paper jam + streaky print output.', 12)
        j4.change_status('received')
        j4.change_status('diagnosis')
        j4.change_status('repairing')
        RepairPart.objects.create(repair_job=j4, product=products['Keyboard USB Standard'],
                                  quantity=1, unit_price=Decimal('650.00'))
        RepairService.objects.create(repair_job=j4, product=cleaning,
                                     amount=Decimal('400.00'),
                                     description='Printer head cleaning')
        j4.change_status('ready')

        # 5) Delivered with invoice (parts+labour, intrastate GST, paid cash)
        j5 = new_job(customers[4], 'Demo Laptop L-220',
                     'Dead battery + slow performance.', 18)
        j5.change_status('received')
        j5.change_status('diagnosis')
        j5.estimated_cost = Decimal('4200.00')
        j5.estimate_status = 'approved'
        j5.estimate_approved_at = timezone.now()
        j5.estimate_approved_by = admin
        j5.save()
        j5.change_status('repairing')
        part = RepairPart.objects.create(repair_job=j5, product=products['SSD 480GB SATA'],
                                         quantity=1, unit_price=Decimal('3200.00'))
        svc = RepairService.objects.create(repair_job=j5, product=labour,
                                           amount=Decimal('800.00'),
                                           description='Battery + SSD replacement, OS install')
        j5.change_status('ready')
        inv = Invoice.objects.create(
            customer=customers[4], date=self._d(4), gst_type='intrastate',
            due_date=self._d(-11), notes='Demo repair invoice',
        )
        InvoiceItem.objects.create(invoice=inv, product=part.product,
                                   description=part.product.name, quantity=Decimal(1),
                                   unit_price=part.unit_price, tax_rate=Decimal('18.00'),
                                   repair_part=part)
        InvoiceItem.objects.create(invoice=inv, product=svc.product,
                                   description=svc.description, quantity=Decimal(1),
                                   unit_price=svc.amount, tax_rate=Decimal('18.00'),
                                   repair_service=svc)
        j5.invoice = inv
        j5.save(update_fields=['invoice'])
        j5.delivered_to_name = customers[4].name
        j5.change_status('delivered')
        inv.refresh_from_db()
        payment = Payment.objects.create(
            direction='received', contact=customers[4], amount=inv.grand_total,
            date=self._d(3), method='cash', bank_account=banks[1],
            description='Demo repair collection',
        )
        PaymentAllocation.objects.create(payment=payment, invoice=inv, amount=inv.grand_total)
        inv.refresh_from_db()
        inv.recompute_payment_state()
        inv.save(update_fields=['balance_due', 'payment_status'])

        # 6) Advance collected at intake (is_advance Payment, portal credit pool)
        j6 = new_job(customers[0], 'Demo Monitor M-24',
                     'Vertical lines on panel.', 2,
                     estimated_cost=Decimal('2500.00'))
        j6.change_status('received')
        j6.change_status('diagnosis')
        Payment.objects.create(
            direction='received', contact=customers[0], amount=Decimal('500.00'),
            date=self._d(2), method='upi', bank_account=banks[0], is_advance=True,
            upi_ref='DEMOUPI0001', description='Demo repair advance (intake)',
        )
        # Settle that advance against Ravi's still-unpaid sales bill so the
        # buyer sees the AdvanceAdjustment flow end-to-end.
        open_ravi = [inv for inv in invoices
                     if inv.customer_id == customers[0].id and inv.balance_due > 0]
        for inv in open_ravi:
            inv.apply_available_advance(note='Demo intake advance settle')
            break

    # ── credit note ────────────────────────────────────────────
    def _credit_note(self, customers, products):
        # Any paid invoice containing a returned physical product works.
        inv = Invoice.objects.filter(
            items__product__name='Monitor 21.5 inch LED',
        ).exclude(payment_status='unpaid').first()
        if not inv:
            inv = Invoice.objects.filter(
                items__product__name='Monitor 21.5 inch LED',
            ).first()
        if not inv:
            return
        item = inv.items.filter(product__name='Monitor 21.5 inch LED').first()
        cn = CreditNote(
            invoice=inv, customer=inv.customer, date=self._d(4),
            reason='sales_return', refund_method='credit',
            notes='Demo sales return — customer changed mind',
        )
        cn.save()
        CreditNoteItem.objects.create(
            credit_note=cn, invoice_item=item, product=item.product,
            quantity_returned=Decimal('1'), unit_price=item.unit_price,
        )
        # Header save after items triggers the CN ledger sync (views/sales.py
        # credit-note flow ka mirror) — CreditNoteItem.save syncs khud nahi karta.
        cn.save()

    # ── direct expenses ────────────────────────────────────────
    def _expenses(self, banks, vendors):
        from accounting.models import get_account
        rent = get_account('5040', 'Shop Rent', 'expense', '5')
        salary = get_account('5041', 'Staff Salary', 'expense', '5')
        Payment.objects.create(
            direction='paid', contact=vendors[0], amount=Decimal('12000.00'),
            date=self._d(8), method='bank', bank_account=banks[0],
            expense_account=rent, description='Demo shop rent (monthly)',
        )
        Payment.objects.create(
            direction='paid', contact=vendors[0], amount=Decimal('9000.00'),
            date=self._d(1), method='bank', bank_account=banks[0],
            expense_account=salary, description='Demo staff salary',
        )
        # Vendor bill settlement (PurchaseAllocation flow) — settle the
        # smallest outstanding bill so demo bank balances stay positive.
        bill = Purchase.objects.filter(paid=False).order_by('grand_total').first()
        if bill:
            settle = Payment.objects.create(
                direction='paid', contact=bill.vendor, amount=bill.grand_total,
                date=self._d(5), method='bank', bank_account=banks[0],
                description=f'Demo vendor payment',
            )
            PurchaseAllocation.objects.create(
                payment=settle, purchase=bill, amount=bill.grand_total,
            )
            bill.recalc_settlement()

    # ── landing content ────────────────────────────────────────
    def _landing_content(self):
        services = [
            ('Chip Level Laptop Repair', 'Motherboard-level fault finding and component repair for all brands.'),
            ('Data Recovery & Backup', 'Recover data from crashed drives and set up automatic backups.'),
            ('OS & Software Setup', 'Fresh Windows/Linux install, drivers, antivirus and data migration.'),
            ('Sales & Upgrades', 'SSD, RAM, peripherals and assembled PCs with warranty.'),
        ]
        for i, (title, desc) in enumerate(services):
            Service.objects.get_or_create(
                title=title,
                defaults={'description': desc, 'icon': 'bi-laptop', 'order': i},
            )
        testimonials = [
            ('Pooja M.', 'Teacher', 'Demo School', 'Got my laptop back same day. Great demo testimonial!', 5),
            ('Rakesh R.', 'Shopkeeper', 'Demo Retail', 'Honest pricing and clear updates on WhatsApp. 5 stars.', 5),
        ]
        for i, (name, desig, comp, text, rating) in enumerate(testimonials):
            Testimonial.objects.get_or_create(
                customer_name=name,
                defaults={'designation': desig, 'company_name': comp,
                          'review_text': text, 'rating': rating, 'order': i},
            )
        faqs = [
            ('How long does a typical repair take?', 'Most chip-level repairs finish in 1-3 days after the estimate is approved.'),
            ('Do I get a warranty on repairs?', 'Demo build: yes, 30-day warranty on replaced parts and labour.'),
            ('Can I track my repair status?', 'Yes — every job gets a no-login tracking link on WhatsApp and email.'),
        ]
        for i, (q, a) in enumerate(faqs):
            FAQ.objects.get_or_create(question=q, defaults={'answer': a, 'order': i})

    # ── messages + notifications ───────────────────────────────
    def _messages_and_notifications(self, admin):
        ContactMessage.objects.get_or_create(
            email='prospective.buyer@example.com',
            defaults={'name': 'Prospective Buyer', 'subject': 'Demo enquiry',
                      'message': 'I am trying the demo software and would like pricing for the production site.',
                      'status': 'new'},
        )
        low = Product.objects.filter(current_stock__lte=random.randint(5, 6), is_service=False).first()
        if low:
            Notification.objects.get_or_create(
                recipient=admin, title=f"Low stock: {low.name}",
                defaults={'message': f'{low.name} stock ({low.current_stock}) is at/below the '
                                     f'threshold ({low.low_stock_threshold}). Reorder from the Purchases page.',
                          'notification_type': 'warning', 'category': 'stock',
                          'link': '/products/'},
            )
        ready = RepairJob.objects.filter(status='ready').first()
        if ready:
            Notification.objects.get_or_create(
                recipient=admin, title=f"Job {ready.job_number} ready",
                defaults={'message': f'{ready.device_model} is ready for delivery to {ready.customer.name}.',
                          'notification_type': 'success', 'category': 'repairs',
                          'link': f'/repairs/{ready.pk}/'},
            )
