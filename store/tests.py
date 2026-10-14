import os
import hashlib
import hmac
import json
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from shopease.settings import _build_host_security_settings
from store.models import CartItem, Category, FashionCategory, Order, OrderItem, Product, ProductVariant, SubCategory, UploadedImage


class PublicPolicyPagesTests(TestCase):
    def test_policy_pages_render(self):
        cases = [
            ('privacy_policy', 'Privacy Policy'),
            ('refund_policy', 'Refund and Cancellation Policy'),
            ('terms_and_conditions', 'Terms and Conditions'),
        ]

        for route_name, expected_text in cases:
            with self.subTest(route_name=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected_text)

    def test_home_footer_exposes_customer_policy_links(self):
        response = self.client.get(reverse('home'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('privacy_policy'))
        self.assertContains(response, reverse('refund_policy'))
        self.assertContains(response, reverse('terms_and_conditions'))

    def test_home_page_renders_without_product_context_errors(self):
        response = self.client.get(reverse('home'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ShopEase')


class HostSecuritySettingsTests(TestCase):
    def test_payment_callback_domain_is_added_to_allowed_hosts_and_csrf_origins(self):
        with patch.dict(os.environ, {
            'ALLOWED_HOSTS': '127.0.0.1,localhost',
            'CSRF_TRUSTED_ORIGINS': 'https://existing.example.com',
            'PAYMENT_CALLBACK_BASE_URL': 'https://shop.example.com/payments',
        }, clear=False):
            allowed_hosts, trusted_origins = _build_host_security_settings()

        self.assertIn('shop.example.com', allowed_hosts)
        self.assertIn('https://shop.example.com', trusted_origins)
        self.assertIn('https://existing.example.com', trusted_origins)


class PublicSecurityTests(TestCase):
    def test_login_rejects_external_next_redirects(self):
        user = User.objects.create_user(
            username='buyer@example.com',
            email='buyer@example.com',
            password='secret123',
        )

        with TemporaryDirectory(dir=r'C:\tmp') as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                reverse('login'),
                {
                    'email': user.email,
                    'password': 'secret123',
                    'next': 'https://evil.example/phish',
                },
            )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, reverse('home'))

    def test_public_products_api_is_read_only(self):
        response = self.client.post(reverse('product-list'), {})

        self.assertEqual(response.status_code, 405)

class PublicFashionMaintenanceTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(category_name='mens')
        self.root = FashionCategory.objects.create(
            name='Mens Root',
            slug='mens-root',
            root_category=self.category,
            level=0,
            sort_order=0,
            is_active=True,
        )
        self.maintenance_category = FashionCategory.objects.create(
            name='Casual Wear',
            slug='casual-wear',
            parent=self.root,
            root_category=self.category,
            level=1,
            sort_order=1,
            is_active=False,
            is_under_maintenance=True,
        )
        self.normal_category = FashionCategory.objects.create(
            name='Formal Wear',
            slug='formal-wear',
            parent=self.root,
            root_category=self.category,
            level=1,
            sort_order=2,
            is_active=True,
            is_under_maintenance=False,
        )
        self.maintenance_subcategory = SubCategory.objects.create(
            category=self.category,
            subcategory_name='Casual Wear',
        )
        self.normal_subcategory = SubCategory.objects.create(
            category=self.category,
            subcategory_name='Formal Wear',
        )
        Product.objects.create(
            product_name='Hidden Maintenance Shirt',
            brand='ShopEase',
            category=self.category,
            subcategory=self.maintenance_subcategory,
            fashion_category=self.maintenance_category,
            sku='MAINT-SHIRT-1',
            price=Decimal('999.00'),
            discount_percent=Decimal('0.00'),
            is_active=True,
        )
        Product.objects.create(
            product_name='Visible Formal Shirt',
            brand='ShopEase',
            category=self.category,
            subcategory=self.normal_subcategory,
            fashion_category=self.normal_category,
            sku='FORMAL-SHIRT-1',
            price=Decimal('1299.00'),
            discount_percent=Decimal('0.00'),
            is_active=True,
        )

    def test_maintenance_category_card_stays_visible_and_clickable(self):
        response = self.client.get('/men/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Casual Wear')
        self.assertContains(response, 'href="/men/casual-wear/"')
        self.assertContains(response, 'Maintenance')
        self.assertNotContains(response, '.gallery-grid::before')

    def test_maintenance_category_click_shows_message_not_products(self):
        response = self.client.get('/men/casual-wear/')

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'store/under-maintenance.html')
        self.assertContains(response, 'Casual Wear is currently under maintenance')
        self.assertNotContains(response, 'Hidden Maintenance Shirt')

    def test_normal_category_click_still_shows_product_listing(self):
        response = self.client.get('/men/formal-wear/')

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'store/product-listing-generic.html')
        self.assertContains(response, 'Visible Formal Shirt')


class AdminCategoryImageUploadTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user(
            username='category-manager',
            email='category-manager@example.com',
            password='secret123',
            is_staff=True,
        )
        self.root_category, _ = Category.objects.get_or_create(category_name='mens')
        self.root, _ = FashionCategory.objects.update_or_create(
            parent=None,
            root_category=self.root_category,
            slug='men',
            defaults={
                'name': 'Men',
                'level': 0,
                'sort_order': 1,
                'is_active': True,
            },
        )
        self.casual, _ = FashionCategory.objects.update_or_create(
            root_category=self.root_category,
            parent=self.root,
            slug='casual-wear',
            defaults={
                'name': 'Casual Wear',
                'level': 1,
                'sort_order': 1,
                'image': 'images/old-casual.jpg',
                'is_active': True,
            },
        )

    def test_category_upload_is_saved_to_media_and_database_for_ajax(self):
        self.client.force_login(self.admin_user)
        upload = SimpleUploadedFile(
            'casual-card.jpg',
            b'fake image content',
            content_type='image/jpeg',
        )

        with TemporaryDirectory(dir=r'C:\tmp') as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                reverse('admin_categories'),
                {
                    'action': 'save_fashion_category',
                    'fashion_category_id': self.casual.fashion_category_id,
                    'name': 'Casual Wear',
                    'root_category_id': self.root_category.category_id,
                    'parent_id': self.root.fashion_category_id,
                    'sort_order': '1',
                    'description': 'Permanent casual card image',
                    'image': self.casual.image,
                    'banner_image': '',
                    'is_active': 'on',
                    'image_upload': upload,
                },
                HTTP_X_REQUESTED_WITH='XMLHttpRequest',
                HTTP_ACCEPT='application/json',
            )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload['success'])
            self.casual.refresh_from_db()
            self.assertTrue(self.casual.image.startswith('dbuploads/assets/casual-wear'))
            stored_image = UploadedImage.objects.get(path=self.casual.image)
            self.assertEqual(bytes(stored_image.data), b'fake image content')
            self.assertEqual(stored_image.content_type, 'image/jpeg')
            self.assertEqual(payload['category']['image'], self.casual.image)
            self.assertIn(self.casual.image, payload['category']['image_url'])

            response = self.client.get(reverse('men_root'))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, self.casual.image)

    def test_category_upload_rejects_invalid_format_as_json(self):
        self.client.force_login(self.admin_user)
        upload = SimpleUploadedFile(
            'not-an-image.gif',
            b'gif data',
            content_type='image/gif',
        )

        response = self.client.post(
            reverse('admin_categories'),
            {
                'action': 'save_fashion_category',
                'fashion_category_id': self.casual.fashion_category_id,
                'name': 'Casual Wear',
                'root_category_id': self.root_category.category_id,
                'parent_id': self.root.fashion_category_id,
                'sort_order': '1',
                'is_active': 'on',
                'image_upload': upload,
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            HTTP_ACCEPT='application/json',
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('Only JPG', payload['message'])

    def test_category_replacement_updates_database_path_and_removes_old_upload(self):
        self.client.force_login(self.admin_user)
        first_upload = SimpleUploadedFile('first.webp', b'first image', content_type='image/webp')
        second_upload = SimpleUploadedFile('second.png', b'second image', content_type='image/png')

        first_response = self.client.post(
            reverse('admin_categories'),
            {
                'action': 'save_fashion_category',
                'fashion_category_id': self.casual.fashion_category_id,
                'name': 'Casual Wear',
                'root_category_id': self.root_category.category_id,
                'parent_id': self.root.fashion_category_id,
                'sort_order': '1',
                'is_active': 'on',
                'image': self.casual.image,
                'image_upload': first_upload,
            },
            HTTP_ACCEPT='application/json',
        )
        self.assertEqual(first_response.status_code, 200)
        old_path = first_response.json()['category']['image']
        self.assertTrue(UploadedImage.objects.filter(path=old_path).exists())

        second_response = self.client.post(
            reverse('admin_categories'),
            {
                'action': 'save_fashion_category',
                'fashion_category_id': self.casual.fashion_category_id,
                'name': 'Casual Wear',
                'root_category_id': self.root_category.category_id,
                'parent_id': self.root.fashion_category_id,
                'sort_order': '1',
                'is_active': 'on',
                'image': old_path,
                'image_upload': second_upload,
            },
            HTTP_ACCEPT='application/json',
        )
        self.assertEqual(second_response.status_code, 200)
        new_path = second_response.json()['category']['image']
        self.assertNotEqual(old_path, new_path)
        self.assertFalse(UploadedImage.objects.filter(path=old_path).exists())
        self.assertEqual(bytes(UploadedImage.objects.get(path=new_path).data), b'second image')
        self.casual.refresh_from_db()
        self.assertEqual(self.casual.image, new_path)

    def test_category_save_rejects_local_and_temporary_image_paths(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse('admin_categories'),
            {
                'action': 'save_fashion_category',
                'fashion_category_id': self.casual.fashion_category_id,
                'name': 'Casual Wear',
                'root_category_id': self.root_category.category_id,
                'parent_id': self.root.fashion_category_id,
                'sort_order': '1',
                'is_active': 'on',
                'image': r'C:\Users\saiva\Desktop\casual.jpg',
                'banner_image': 'blob:https://example.com/123',
            },
            HTTP_ACCEPT='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.casual.refresh_from_db()
        self.assertIsNone(self.casual.image)
        self.assertIsNone(self.casual.banner_image)
    def test_category_api_create_update_get_and_delete_return_json(self):
        self.client.force_login(self.admin_user)

        create_response = self.client.post(
            reverse('admin_category_api'),
            {
                'action': 'save_fashion_category',
                'name': 'QA Casual Card',
                'root_category_id': self.root_category.category_id,
                'parent_id': self.root.fashion_category_id,
                'sort_order': '3',
                'description': 'Created through JSON API',
                'is_active': 'on',
            },
            HTTP_ACCEPT='application/json',
        )
        self.assertEqual(create_response.status_code, 200)
        create_payload = create_response.json()
        self.assertTrue(create_payload['success'])
        self.assertEqual(create_payload['message'], 'Operation completed successfully')
        created_id = create_payload['category']['id']

        list_response = self.client.get(reverse('admin_category_api'), HTTP_ACCEPT='application/json')
        self.assertEqual(list_response.status_code, 200)
        self.assertTrue(list_response.json()['success'])
        self.assertIn(created_id, [category['id'] for category in list_response.json()['categories']])

        update_response = self.client.post(
            reverse('admin_category_api'),
            {
                'action': 'save_fashion_category',
                'fashion_category_id': created_id,
                'name': 'QA Casual Card Updated',
                'root_category_id': self.root_category.category_id,
                'parent_id': self.root.fashion_category_id,
                'sort_order': '4',
                'description': 'Updated through JSON API',
                'is_active': 'on',
            },
            HTTP_ACCEPT='application/json',
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()['category']['name'], 'QA Casual Card Updated')

        delete_response = self.client.delete(
            reverse('admin_category_detail_api', kwargs={'fashion_category_id': created_id}),
            HTTP_ACCEPT='application/json',
        )
        self.assertEqual(delete_response.status_code, 200)
        self.assertTrue(delete_response.json()['success'])
        self.assertFalse(FashionCategory.objects.filter(fashion_category_id=created_id).exists())

    def test_admin_save_can_clear_maintenance_and_republish_card(self):
        self.client.force_login(self.admin_user)
        self.casual.is_active = False
        self.casual.is_under_maintenance = True
        self.casual.save(update_fields=['is_active', 'is_under_maintenance'])

        response = self.client.post(
            reverse('admin_categories'),
            {
                'action': 'save_fashion_category',
                'fashion_category_id': self.casual.fashion_category_id,
                'name': 'Casual Wear',
                'root_category_id': self.root_category.category_id,
                'parent_id': self.root.fashion_category_id,
                'sort_order': '1',
                'is_active': 'on',
            },
            HTTP_ACCEPT='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        self.casual.refresh_from_db()
        self.assertTrue(self.casual.is_active)
        self.assertFalse(self.casual.is_under_maintenance)

        storefront_response = self.client.get('/mens/casual-wear/')
        self.assertEqual(storefront_response.status_code, 200)
        self.assertTemplateUsed(storefront_response, 'store/product-listing-generic.html')
        self.assertNotContains(storefront_response, 'currently under maintenance')

    def test_ajax_auth_failure_returns_json_not_html(self):

        response = self.client.post(
            reverse('admin_categories'),
            {'action': 'save_fashion_category'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            HTTP_ACCEPT='application/json',
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertFalse(response.json()['success'])

class AdminProductUpdateTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user(
            username='manager',
            email='manager@example.com',
            password='secret123',
            is_staff=True,
        )
        self.category, _ = Category.objects.get_or_create(category_name='mens')
        self.subcategory = SubCategory.objects.create(
            category=self.category,
            subcategory_name='shirts',
        )
        self.product = Product.objects.create(
            product_name='Office Shirt',
            brand='ShopEase',
            category=self.category,
            subcategory=self.subcategory,
            sku='OFFICE-SHIRT-1',
            price=Decimal('1000.00'),
            discount_percent=Decimal('10.00'),
            description='Before update',
            is_active=True,
        )
        ProductVariant.objects.create(
            product=self.product,
            size='M',
            color='Blue',
            quantity=5,
            image1='men_shirt_black_front.jpg',
        )

    def test_product_save_recalculates_offer_price(self):
        self.product.price = Decimal('1200.00')
        self.product.discount_percent = Decimal('25.00')
        self.product.save()
        self.product.refresh_from_db()

        self.assertEqual(self.product.offer_price, Decimal('900.00'))

    def test_admin_edit_updates_storefront_values(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse('admin_product_edit', args=[self.product.product_id]),
            {
                'product_name': 'Office Shirt Updated',
                'brand': 'ShopEase Pro',
                'category_id': self.category.category_id,
                'subcategory_id': self.subcategory.subcategory_id,
                'sku': self.product.sku,
                'price': '1500.00',
                'discount_percent': '20.00',
                'description': 'Updated from admin',
                'size': 'L',
                'quantity': '9',
                'color': 'Black',
                'image1': 'men_shirt_black_front.jpg',
                'image2': '',
                'image3': '',
                'image4': '',
                'is_active': 'on',
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        variant = ProductVariant.objects.get(product=self.product)

        self.assertEqual(self.product.product_name, 'Office Shirt Updated')
        self.assertEqual(self.product.brand, 'ShopEase Pro')
        self.assertEqual(self.product.offer_price, Decimal('1200.00'))
        self.assertEqual(variant.size, 'L')
        self.assertEqual(variant.quantity, 9)

        home_response = self.client.get(reverse('home'))
        self.assertEqual(home_response.status_code, 200)
        self.assertContains(home_response, 'Office Shirt Updated')
        self.assertContains(home_response, 'Rs. 1200.00')


class RazorpayFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='buyer@example.com',
            email='buyer@example.com',
            password='secret123',
        )
        self.category, _ = Category.objects.get_or_create(category_name='mens')
        self.subcategory = SubCategory.objects.create(
            category=self.category,
            subcategory_name='shirts',
        )
        self.product = Product.objects.create(
            product_name='Live Payment Shirt',
            brand='ShopEase',
            category=self.category,
            subcategory=self.subcategory,
            sku='LIVE-PAYMENT-SHIRT-1',
            price=Decimal('1000.00'),
            discount_percent=Decimal('10.00'),
            description='For Razorpay flow tests',
            is_active=True,
        )
        self.variant = ProductVariant.objects.create(
            product=self.product,
            size='M',
            color='Blue',
            quantity=6,
            image1='men_shirt_black_front.jpg',
        )

    def _create_pending_order(self):
        order = Order.objects.create(
            user=self.user,
            full_name='Buyer Example',
            phone_number='9999999999',
            email='buyer@example.com',
            address='123 Test Street',
            city='Chennai',
            state='Tamil Nadu',
            pincode='600001',
            country='India',
            payment_method='Razorpay',
            payment_status='Created',
            currency='INR',
            subtotal=Decimal('1800.00'),
            total_items=2,
            status='Pending Payment',
            razorpay_order_id='order_live_123',
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            selected_size='M',
            quantity=2,
            unit_price=Decimal('900.00'),
            line_total=Decimal('1800.00'),
        )
        return order

    @override_settings(
        RAZORPAY_MODE='test',
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    @patch('store.views._create_razorpay_order')
    def test_standard_checkout_creates_order_using_cart_total(self, mock_create_razorpay_order):
        mock_create_razorpay_order.return_value = {
            'id': 'order_live_123',
            'currency': 'INR',
        }
        CartItem.objects.create(
            user=self.user,
            product=self.product,
            selected_size='M',
            quantity=2,
        )
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('create_razorpay_checkout'),
            {
                'full_name': 'Buyer Example',
                'phone_number': '9999999999',
                'email': 'buyer@example.com',
                'address': '123 Test Street',
                'city': 'Chennai',
                'state': 'Tamil Nadu',
                'pincode': '600001',
                'country': 'India',
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['amount'], 180000)
        self.assertEqual(payload['razorpay_order_id'], 'order_live_123')

        order = Order.objects.get(order_id=payload['local_order_id'])
        self.assertEqual(order.subtotal, Decimal('1800.00'))
        self.assertEqual(order.total_items, 2)
        self.assertEqual(order.payment_status, 'Created')
        self.assertEqual(order.razorpay_order_id, 'order_live_123')

    @override_settings(
        RAZORPAY_MODE='test',
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        DEFAULT_FROM_EMAIL='noreply@example.com',
    )
    @patch('store.views._verify_razorpay_signature', return_value=True)
    @patch('store.views._fetch_razorpay_payment')
    def test_verify_payment_uses_saved_order_total_and_confirms_order(self, mock_fetch_razorpay_payment, mock_verify_signature):
        order = self._create_pending_order()
        CartItem.objects.create(
            user=self.user,
            product=self.product,
            selected_size='M',
            quantity=2,
        )
        mock_fetch_razorpay_payment.return_value = {
            'status': 'captured',
            'amount': 180000,
            'order_id': 'order_live_123',
            'currency': 'INR',
        }
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('verify_razorpay_payment'),
            {
                'local_order_id': order.order_id,
                'razorpay_order_id': 'order_live_123',
                'razorpay_payment_id': 'pay_live_123',
                'razorpay_signature': 'signature_123',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('order_success', kwargs={'order_id': order.order_id}))

        order.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(order.payment_status, 'Captured')
        self.assertEqual(order.payment_method, 'Razorpay')
        self.assertEqual(order.razorpay_payment_id, 'pay_live_123')
        self.assertEqual(order.status, 'Placed')
        self.assertEqual(self.variant.quantity, 4)
        self.assertFalse(CartItem.objects.filter(user=self.user).exists())
        mock_verify_signature.assert_called_once()

    @override_settings(
        RAZORPAY_WEBHOOK_SECRET='webhook_test_secret',
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        DEFAULT_FROM_EMAIL='noreply@example.com',
    )
    def test_webhook_marks_captured_order_paid(self):
        order = self._create_pending_order()
        CartItem.objects.create(
            user=self.user,
            product=self.product,
            selected_size='M',
            quantity=2,
        )
        payload = {
            'event': 'order.paid',
            'payload': {
                'payment': {
                    'entity': {
                        'id': 'pay_live_123',
                        'amount': 180000,
                        'currency': 'INR',
                        'status': 'captured',
                        'order_id': 'order_live_123',
                    },
                },
                'order': {
                    'entity': {
                        'id': 'order_live_123',
                        'amount_paid': 180000,
                        'currency': 'INR',
                        'status': 'paid',
                    },
                },
            },
        }
        raw_body = json.dumps(payload).encode('utf-8')
        signature = hmac.new(
            b'webhook_test_secret',
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        response = self.client.post(
            reverse('razorpay_webhook'),
            data=raw_body,
            content_type='application/json',
            HTTP_X_RAZORPAY_SIGNATURE=signature,
            HTTP_X_RAZORPAY_EVENT_ID='event_123',
        )

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(order.payment_status, 'Paid')
        self.assertEqual(order.razorpay_payment_id, 'pay_live_123')
        self.assertEqual(order.status, 'Placed')
        self.assertEqual(self.variant.quantity, 4)
        self.assertFalse(CartItem.objects.filter(user=self.user).exists())

    @override_settings(
        RAZORPAY_MODE='test',
        RAZORPAY_KEY_ID='rzp_test_example',
        RAZORPAY_KEY_SECRET='test_secret',
    )
    @patch('store.views._fetch_razorpay_payment_link')
    def test_payment_link_callback_rejects_wrong_logged_in_user_before_finalizing(self, mock_fetch_payment_link):
        owner = self.user
        stranger = User.objects.create_user(
            username='intruder@example.com',
            email='intruder@example.com',
            password='secret123',
        )
        order = Order.objects.create(
            user=owner,
            full_name='Buyer Example',
            phone_number='9999999999',
            email='buyer@example.com',
            address='123 Test Street',
            city='Chennai',
            state='Tamil Nadu',
            pincode='600001',
            country='India',
            payment_method='Razorpay Payment Link',
            payment_status='Created',
            currency='INR',
            subtotal=Decimal('1800.00'),
            total_items=2,
            status='Pending Payment',
            razorpay_order_id='plink_live_123',
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            selected_size='M',
            quantity=2,
            unit_price=Decimal('900.00'),
            line_total=Decimal('1800.00'),
        )
        mock_fetch_payment_link.return_value = {
            'status': 'paid',
            'amount_paid': 180000,
            'currency': 'INR',
            'reference_id': f'shopease_{order.order_id}',
            'payments': [{'payment_id': 'pay_live_123'}],
        }
        self.client.force_login(stranger)

        signature_payload = f'plink_live_123|shopease_{order.order_id}|paid|pay_live_123'
        signature = hmac.new(
            b'test_secret',
            signature_payload.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()

        response = self.client.get(
            reverse('razorpay_payment_link_callback'),
            {
                'local_order_id': order.order_id,
                'razorpay_payment_link_id': 'plink_live_123',
                'razorpay_payment_link_reference_id': f'shopease_{order.order_id}',
                'razorpay_payment_link_status': 'paid',
                'razorpay_payment_id': 'pay_live_123',
                'razorpay_signature': signature,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('home'))
        order.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(order.payment_status, 'Created')
        self.assertEqual(order.status, 'Pending Payment')
        self.assertEqual(self.variant.quantity, 6)


class DuplicateCategoryNameTests(TestCase):
    """The catalog seed contains both "Mens" and "mens" (and Womens/Kids).

    A name-based ``get()`` raised MultipleObjectsReturned, which turned the
    category pages into 500s, so these lookups must tolerate duplicate
    case-variant rows instead of assuming uniqueness.
    """

    def setUp(self):
        for name in ('mens', 'womens', 'kids'):
            Category.objects.get_or_create(category_name=name)
            Category.objects.create(category_name=name.capitalize())

    def test_duplicate_case_variant_categories_exist(self):
        for name in ('mens', 'womens', 'kids'):
            with self.subTest(name=name):
                self.assertEqual(
                    Category.objects.filter(category_name__iexact=name).count(),
                    2,
                )

    def test_category_view_does_not_500_on_duplicate_names(self):
        response = self.client.get('/mens/')
        self.assertNotEqual(response.status_code, 500)

    def test_fashion_category_path_does_not_500_on_duplicate_names(self):
        for path in ('/mens/accessories/', '/womens/ethnicwear/', '/kids/accessories/'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertNotEqual(response.status_code, 500)

    def test_preferred_category_picks_lowest_id(self):
        from store.views import _preferred_category_by_name

        expected = (
            Category.objects.filter(category_name__iexact='mens')
            .order_by('category_id')
            .first()
        )
        self.assertEqual(
            _preferred_category_by_name('mens').category_id, expected.category_id
        )

    def test_preferred_category_returns_none_for_unknown_name(self):
        from store.views import _preferred_category_by_name

        self.assertIsNone(_preferred_category_by_name('definitely-not-a-category'))
        self.assertIsNone(_preferred_category_by_name(''))
