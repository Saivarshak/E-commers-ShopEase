from pathlib import Path
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import mimetypes
import json
import time
from uuid import uuid4

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.core.mail import EmailMultiAlternatives
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.utils import OperationalError, ProgrammingError
from django.db.models import Max, Q, Sum
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.urls import reverse
from django.shortcuts import render, get_object_or_404, redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
import requests
import urllib.parse
# Browser caching for assets served through the dynamic asset endpoint.
# Keep this moderate while filenames are not content-hashed. If you later use
# versioned/hashed filenames, STATIC_ASSET_MAX_AGE can be increased to a year.
STATIC_ASSET_MAX_AGE = 60 * 60 * 24 * 7  # 7 days
# Uploaded assets receive a random token in their filename. Replacements use a
# new path, so a long immutable cache is safe and prevents repeat downloads.
UPLOAD_ASSET_MAX_AGE = 60 * 60 * 24 * 365


def _set_asset_cache_headers(response, max_age):
    immutable = ', immutable' if max_age == UPLOAD_ASSET_MAX_AGE else ''
    response['Cache-Control'] = f'public, max-age={max_age}, must-revalidate{immutable}'
    return response


from .models import (
    Product,
    Category,
    SubCategory,
    ProductVariant,
    CartItem,
    Order,
    OrderItem,
    WishlistItem,
    UserAddress,
    FashionCategory,
    MenCategory,
    WomenCategory,
    KidsCategory,
    SiteAsset,
    PageAsset,
    HomeContent,
    UploadedImage,
)


def health_check(request):
    """Return readiness only when Django can reach its configured database."""
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except (OperationalError, ProgrammingError):
        response = JsonResponse({'status': 'unhealthy', 'database': 'unavailable'}, status=503)
    else:
        response = HttpResponse('OK', content_type='text/plain')

    response['Cache-Control'] = 'no-store'
    return response


def _extract_profile_photo_url(value):
    """Return a valid Google profile-photo URL from OAuth data."""
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.startswith(('https://', 'http://')):
            return candidate
        return ''
    if isinstance(value, dict):
        for key in ('picture', 'image', 'picture_url', 'image_url', 'profile_image_url'):
            photo_url = _extract_profile_photo_url(value.get(key))
            if photo_url:
                return photo_url
        for nested_value in value.values():
            photo_url = _extract_profile_photo_url(nested_value)
            if photo_url:
                return photo_url
    elif isinstance(value, (list, tuple)):
        for nested_value in value:
            photo_url = _extract_profile_photo_url(nested_value)
            if photo_url:
                return photo_url
    return ''


def _set_session_profile_photo(request, user, profile_photo_url=''):
    """Fetch and store the user's Google profile photo URL in session.
    Priority: OAuth callback data > stored social-auth data > UI Avatars fallback."""
    photo_url = _extract_profile_photo_url(profile_photo_url)
    try:
        from social_django.models import UserSocialAuth
        social = UserSocialAuth.objects.filter(user=user, provider='google-oauth2').first()
        if not photo_url and social and social.extra_data:
            photo_url = _extract_profile_photo_url(social.extra_data)
        if photo_url and social and isinstance(social.extra_data, dict) and not social.extra_data.get('picture'):
            social.extra_data['picture'] = photo_url
            social.save(update_fields=['extra_data'])
    except Exception:
        pass
    if not photo_url:
        display_name = user.get_full_name() or (user.email or user.username or '')
        photo_url = f"https://ui-avatars.com/api/?name={urllib.parse.quote(display_name)}&background=000000&color=ffffff&size=128&font-size=0.45&rounded=true&bold=true"
    request.session['profile_photo_url'] = photo_url
    request.session.modified = True


def _asset_exists(normalized_path):
    normalized = Path((normalized_path or '').replace('\\', '/').lstrip('/'))
    if not normalized or normalized.is_absolute() or '..' in normalized.parts:
        return None

    candidate_paths = []
    if normalized.suffix.lower() != '.webp':
        candidate_paths.append(normalized.with_suffix('.webp'))
    candidate_paths.append(normalized)

    image_name = normalized.name
    fallback_image_path = Path('images') / image_name
    if normalized.parts[:1] not in {('images',), (DB_UPLOAD_PREFIX,)}:
        if fallback_image_path.suffix.lower() != '.webp':
            candidate_paths.append(fallback_image_path.with_suffix('.webp'))
        candidate_paths.append(fallback_image_path)

    seen = set()
    for candidate in candidate_paths:
        candidate_key = candidate.as_posix()
        if candidate_key in seen:
            continue
        seen.add(candidate_key)
        if candidate.parts[:1] == (DB_UPLOAD_PREFIX,):
            try:
                if UploadedImage.objects.filter(path=candidate_key).exists():
                    return candidate_key
            except (ProgrammingError, OperationalError):
                continue
        elif candidate.parts[:1] == ('uploads',):
            media_candidate = Path(settings.MEDIA_ROOT) / candidate
            if media_candidate.exists():
                return candidate_key
        elif candidate.parts[:1] == ('images',):
            static_candidate = Path(settings.BASE_DIR) / 'static' / candidate
            if static_candidate.exists():
                return candidate_key
    return None
def _get_site_asset(asset_key, fallback):
    try:
        asset = SiteAsset.objects.filter(asset_key=asset_key, is_active=True).first()
    except (ProgrammingError, OperationalError):
        asset = None

    if asset and asset.image_path:
        normalized = asset.image_path.replace('\\', '/').lstrip('/')
        resolved = _asset_exists(normalized)
        if resolved:
            return resolved
    return _asset_exists(fallback) or fallback


def _get_page_asset(page_key, asset_key, fallback):
    try:
        asset = PageAsset.objects.filter(page_key=page_key, asset_key=asset_key, is_active=True).first()
    except (ProgrammingError, OperationalError):
        asset = None

    if asset and asset.image_path:
        normalized = asset.image_path.replace('\\', '/').lstrip('/')
        resolved = _asset_exists(normalized)
        if resolved:
            return resolved
    return _asset_exists(fallback) or fallback


def _asset_public_url(asset_path):
    normalized = (asset_path or '').replace('\\', '/').lstrip('/')
    if not normalized:
        return ''
    return reverse('store_asset', kwargs={'asset_path': normalized})


def _normalize_image_asset_path(raw_path):
    if _is_forbidden_image_reference(raw_path):
        return ''
    normalized = (raw_path or '').strip().replace('\\', '/').lstrip('/')
    if not normalized:
        return ''
    if normalized.startswith('static/'):
        normalized = normalized[len('static/'):]
    path = Path(normalized)
    if path.is_absolute() or '..' in path.parts:
        return ''
    if path.parts[:1] not in {('images',), ('uploads',), (DB_UPLOAD_PREFIX,)}:
        if '/' in normalized:
            return ''
        normalized = f'images/{normalized}'
    return normalized

def _get_site_asset_url(asset_key, fallback):
    return _asset_public_url(_get_site_asset(asset_key, fallback))


def _get_page_asset_url(page_key, asset_key, fallback):
    return _asset_public_url(_get_page_asset(page_key, asset_key, fallback))


BACKGROUND_ASSET_CHOICES = [
    {
        'scope': 'site',
        'page_key': '',
        'asset_key': 'home_background',
        'label': 'Home Page Background',
        'fallback': 'images/homebg.svg',
    },
    {
        'scope': 'page',
        'page_key': 'category_men',
        'asset_key': 'background',
        'label': "Men's Category Background",
        'fallback': 'images/bg.jpg',
    },
    {
        'scope': 'page',
        'page_key': 'category_women',
        'asset_key': 'background',
        'label': "Women's Category Background",
        'fallback': 'images/womenbg.jpg',
    },
    {
        'scope': 'page',
        'page_key': 'category_kids',
        'asset_key': 'background',
        'label': 'Kids Category Background',
        'fallback': 'images/kidsbg.jpg',
    },
    {
        'scope': 'page',
        'page_key': 'mens_tshirts',
        'asset_key': 'background',
        'label': "Men's T-Shirts Background",
        'fallback': 'images/menstshirts.jpg',
    },
    {
        'scope': 'page',
        'page_key': 'mens_shirts',
        'asset_key': 'background',
        'label': "Men's Shirts Background",
        'fallback': 'images/shirt.jpg',
    },
    {
        'scope': 'page',
        'page_key': 'mens_jeans',
        'asset_key': 'background',
        'label': "Men's Jeans Background",
        'fallback': 'images/jeansbg.jpg',
    },
    {
        'scope': 'page',
        'page_key': 'womens_ethnicware',
        'asset_key': 'background',
        'label': "Women's Ethnic Wear Background",
        'fallback': 'images/ethicwearebg.jpeg',
    },
    {
        'scope': 'page',
        'page_key': 'womens_westernware',
        'asset_key': 'background',
        'label': "Women's Western Wear Background",
        'fallback': 'images/westernware.jpg',
    },
    {
        'scope': 'page',
        'page_key': 'womens_footwear',
        'asset_key': 'background',
        'label': "Women's Footwear Background",
        'fallback': 'images/women.webp',
    },
    {
        'scope': 'page',
        'page_key': 'kids_tshirts',
        'asset_key': 'background',
        'label': "Kids T-Shirts Background",
        'fallback': 'images/kidstshirtsbg.jpeg',
    },
    {
        'scope': 'page',
        'page_key': 'kids_dresses',
        'asset_key': 'background',
        'label': 'Kids Dresses Background',
        'fallback': 'images/kids.webp',
    },
    {
        'scope': 'page',
        'page_key': 'kids_toys',
        'asset_key': 'background',
        'label': 'Kids Toys Background',
        'fallback': 'images/kids.webp',
    },
    {
        'scope': 'page',
        'page_key': 'kids_footwear',
        'asset_key': 'background',
        'label': 'Kids Footwear Background',
        'fallback': 'images/kids.webp',
    },
]


def _get_product_image_path(product, variant):
    category_name = (getattr(product.category, 'category_name', '') or '').lower()
    subcategory_name = (getattr(product.subcategory, 'subcategory_name', '') or '').lower()
    fallback_map = {
        ('mens', 't-shirts'): 'images/shirt.webp',
        ('mens', 'shirts'): 'images/shirt.webp',
        ('mens', 'jeans'): 'images/cargo-pants.webp',
        ('mens', 'accessories'): 'images/mens_accessories.webp',
        ('womens', 'ethnic wear'): 'images/women.webp',
        ('womens', 'western wear'): 'images/women.webp',
        ('womens', 'footwear'): 'images/women.webp',
        ('womens', 'bags & accessories'): 'images/women.webp',
        ('womens', 'bags and accessories'): 'images/women.webp',
        ('kids', 't-shirts'): 'images/kids.webp',
        ('kids', 'kids dresses'): 'images/kids.webp',
        ('kids', 'dresses'): 'images/kids.webp',
        ('kids', 'toys'): 'images/kids.webp',
        ('kids', 'footwear'): 'images/kids.webp',
    }
    fallback_image = fallback_map.get((category_name, subcategory_name), 'images/homebg.svg')
    image_names = [variant.image1, variant.image2, variant.image3, variant.image4] if variant else []

    for image_name in image_names:
        normalized_image_name = _normalize_image_asset_path(image_name)
        if not normalized_image_name:
            continue
        resolved = _asset_exists(normalized_image_name)
        if resolved:
            return resolved
    return _asset_exists(fallback_image) or fallback_image


def _get_razorpay_credentials():
    key_id = (getattr(settings, 'RAZORPAY_KEY_ID', '') or '').strip()
    key_secret = (getattr(settings, 'RAZORPAY_KEY_SECRET', '') or '').strip()
    return key_id, key_secret


def _get_razorpay_webhook_secret():
    return (getattr(settings, 'RAZORPAY_WEBHOOK_SECRET', '') or '').strip()


def _get_razorpay_mode():
    mode = (getattr(settings, 'RAZORPAY_MODE', 'test') or 'test').strip().lower()
    return mode if mode in {'test', 'live'} else 'test'


def _get_razorpay_configuration_error():
    key_id, key_secret = _get_razorpay_credentials()
    mode = _get_razorpay_mode()

    if not key_id or not key_secret:
        return f'Razorpay is not configured. Add your {mode} key ID and secret in the environment.'

    expected_prefix = f'rzp_{mode}_'
    if not key_id.startswith(expected_prefix):
        return (
            f'Razorpay mode is set to {mode}, but the key id does not look like a {mode} key. '
            f'Please update RAZORPAY_MODE or RAZORPAY_KEY_ID.'
        )

    return ''


def _is_razorpay_configured():
    return not _get_razorpay_configuration_error()


def _get_razorpay_override_payment_link():
    return (getattr(settings, 'RAZORPAY_PAYMENT_LINK_OVERRIDE_URL', '') or '').strip()


def _build_payment_callback_url(request, order_id):
    callback_path = f"{reverse('razorpay_payment_link_callback')}?local_order_id={order_id}"
    base_url = (getattr(settings, 'PAYMENT_CALLBACK_BASE_URL', '') or '').strip().rstrip('/')
    if base_url:
        return f"{base_url}{callback_path}"
    return request.build_absolute_uri(callback_path)


def _build_razorpay_receipt(user_id):
    return f"rcpt_{user_id}_{int(time.time())}"[:40]


def _create_razorpay_order(amount_paise, receipt, notes=None):
    key_id, key_secret = _get_razorpay_credentials()
    if not key_id or not key_secret:
        raise ValueError('Razorpay keys are not configured.')

    response = requests.post(
        'https://api.razorpay.com/v1/orders',
        auth=(key_id, key_secret),
        json={
            'amount': amount_paise,
            'currency': 'INR',
            'receipt': receipt,
            'notes': notes or {},
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def _create_razorpay_payment_link(amount_paise, callback_url, reference_id, description, customer=None, notes=None):
    key_id, key_secret = _get_razorpay_credentials()
    if not key_id or not key_secret:
        raise ValueError('Razorpay keys are not configured.')

    payload = {
        'amount': amount_paise,
        'currency': 'INR',
        'accept_partial': False,
        'description': description,
        'reference_id': reference_id,
        'callback_url': callback_url,
        'callback_method': 'get',
        'notify': {
            'sms': False,
            'email': False,
        },
        'reminder_enable': False,
        'notes': notes or {},
    }

    if customer:
        payload['customer'] = customer

    response = requests.post(
        'https://api.razorpay.com/v1/payment_links',
        auth=(key_id, key_secret),
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def _fetch_razorpay_payment_link(payment_link_id):
    key_id, key_secret = _get_razorpay_credentials()
    if not key_id or not key_secret:
        raise ValueError('Razorpay keys are not configured.')

    response = requests.get(
        f'https://api.razorpay.com/v1/payment_links/{payment_link_id}',
        auth=(key_id, key_secret),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def _fetch_razorpay_payment(payment_id):
    key_id, key_secret = _get_razorpay_credentials()
    if not key_id or not key_secret:
        raise ValueError('Razorpay keys are not configured.')

    response = requests.get(
        f'https://api.razorpay.com/v1/payments/{payment_id}',
        auth=(key_id, key_secret),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def _friendly_razorpay_error(exc):
    message = str(exc)
    lowered = message.lower()
    if 'failed to establish a new connection' in lowered or 'unable to connect to proxy' in lowered or 'max retries exceeded' in lowered:
        return 'The server could not reach Razorpay right now. Please check internet or firewall access on this machine, or use the Razorpay Link / QR payment option.'
    return message


def _is_email_configured():
    backend = (getattr(settings, 'EMAIL_BACKEND', '') or '').strip()
    if backend.endswith('console.EmailBackend'):
        return True
    return bool(
        (getattr(settings, 'EMAIL_HOST', '') or '').strip()
        and (getattr(settings, 'DEFAULT_FROM_EMAIL', '') or '').strip()
    )


def _build_invoice_email(order):
    subject = f'ShopEase Invoice for Order #{order.order_id}'
    text_body = (
        f'Hello {order.full_name},\n\n'
        f'Your payment was successful and your order has been confirmed.\n\n'
        f'Invoice Details:\n'
        f'Order ID: #{order.order_id}\n'
        f'Payment Status: {order.payment_status}\n'
        f'Payment Method: {order.payment_method}\n'
        f'Total Amount: Rs. {order.subtotal}\n'
        f'Phone Number: {order.phone_number}\n'
        f'Delivery Address: {order.address}, {order.city}, {order.state} - {order.pincode}, {order.country}\n\n'
        f'Thank you for shopping with ShopEase.'
    )
    html_body = (
        f'<p>Hello {order.full_name},</p>'
        f'<p>Your payment was successful and your order has been confirmed.</p>'
        f'<h3>Invoice Details</h3>'
        f'<ul>'
        f'<li><strong>Order ID:</strong> #{order.order_id}</li>'
        f'<li><strong>Payment Status:</strong> {order.payment_status}</li>'
        f'<li><strong>Payment Method:</strong> {order.payment_method}</li>'
        f'<li><strong>Total Amount:</strong> Rs. {order.subtotal}</li>'
        f'<li><strong>Phone Number:</strong> {order.phone_number}</li>'
        f'<li><strong>Delivery Address:</strong> {order.address}, {order.city}, {order.state} - {order.pincode}, {order.country}</li>'
        f'</ul>'
        f'<p>Thank you for shopping with ShopEase.</p>'
    )
    return subject, text_body, html_body


def _send_order_invoice_email(order):
    recipient = (order.email or '').strip()
    if not recipient or not _is_email_configured():
        return False

    subject, text_body, html_body = _build_invoice_email(order)
    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', ''),
        to=[recipient],
    )
    message.attach_alternative(html_body, 'text/html')
    message.send(fail_silently=False)
    return True


def _store_invoice_delivery_notice(request, order, email_sent):
    request.session['invoice_delivery_notice'] = {
        'order_id': order.order_id,
        'email': order.email,
        'phone_number': order.phone_number,
        'email_sent': bool(email_sent),
    }


def _get_default_phone_number(request):
    invoice_delivery_notice = request.session.get('invoice_delivery_notice') or {}
    return (invoice_delivery_notice.get('phone_number') or '').strip()


def _verify_razorpay_signature(order_id, payment_id, signature):
    _, key_secret = _get_razorpay_credentials()
    generated_signature = hmac.new(
        key_secret.encode(),
        f'{order_id}|{payment_id}'.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(generated_signature, signature)


def _verify_razorpay_payment_link_signature(payment_link_id, reference_id, payment_link_status, payment_id, signature):
    _, key_secret = _get_razorpay_credentials()
    payload = f'{payment_link_id}|{reference_id}|{payment_link_status}|{payment_id}'
    generated_signature = hmac.new(
        key_secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(generated_signature, signature)


def _verify_razorpay_webhook_signature(raw_body, signature):
    webhook_secret = _get_razorpay_webhook_secret()
    if not webhook_secret or not signature:
        return False

    generated_signature = hmac.new(
        webhook_secret.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(generated_signature, signature)


def _extract_order_id_from_reference_id(reference_id):
    if not reference_id:
        return ''

    expected_prefix = 'shopease_'
    if not reference_id.startswith(expected_prefix):
        return ''

    return reference_id[len(expected_prefix):].strip()


def _amount_to_paise(amount):
    return int((Decimal(amount or 0) * Decimal('100')).quantize(Decimal('1')))


def _finalize_paid_order(order, request=None, payment_method='Razorpay Payment Link', payment_status='Paid', payment_id=''):
    already_paid = (order.payment_status or '').strip().lower() in {'paid', 'authorized', 'captured'}

    if not already_paid:
        for item in order.items.select_related('product').all():
            variant_queryset = ProductVariant.objects.select_for_update().filter(product=item.product)
            if item.selected_size:
                variant_queryset = variant_queryset.filter(size__iexact=item.selected_size)
            variant = variant_queryset.order_by('variant_id').first()
            if variant:
                variant.quantity = max(0, (variant.quantity or 0) - item.quantity)
                variant.save(update_fields=['quantity'])

    order.payment_method = payment_method
    order.payment_status = payment_status
    order.status = 'Placed'
    if payment_id:
        order.razorpay_payment_id = payment_id
    order.save(update_fields=['payment_method', 'payment_status', 'status', 'razorpay_payment_id'])

    CartItem.objects.filter(user=order.user).delete()
    if request is not None:
        _set_session_cart(request, {})

    invoice_email_sent = False
    if not already_paid:
        try:
            invoice_email_sent = _send_order_invoice_email(order)
        except Exception:
            invoice_email_sent = False

    if request is not None:
        _store_invoice_delivery_notice(request, order, invoice_email_sent or already_paid)


def _update_order_payment_record(
    order,
    *,
    payment_method=None,
    payment_status=None,
    currency='INR',
    razorpay_order_id='',
    razorpay_payment_id='',
    razorpay_signature='',
    status=None,
):
    update_fields = []

    if payment_method is not None and order.payment_method != payment_method:
        order.payment_method = payment_method
        update_fields.append('payment_method')

    if payment_status is not None and order.payment_status != payment_status:
        order.payment_status = payment_status
        update_fields.append('payment_status')

    if status is not None and order.status != status:
        order.status = status
        update_fields.append('status')

    if currency and order.currency != currency:
        order.currency = currency
        update_fields.append('currency')

    if razorpay_order_id and order.razorpay_order_id != razorpay_order_id:
        order.razorpay_order_id = razorpay_order_id
        update_fields.append('razorpay_order_id')

    if razorpay_payment_id and order.razorpay_payment_id != razorpay_payment_id:
        order.razorpay_payment_id = razorpay_payment_id
        update_fields.append('razorpay_payment_id')

    if razorpay_signature and order.razorpay_signature != razorpay_signature:
        order.razorpay_signature = razorpay_signature
        update_fields.append('razorpay_signature')

    if update_fields:
        order.save(update_fields=update_fields)


def _get_order_from_webhook_payload(payload):
    payload = payload or {}
    payment_entity = ((payload.get('payment') or {}).get('entity') or {})
    order_entity = ((payload.get('order') or {}).get('entity') or {})
    payment_link_entity = ((payload.get('payment_link') or {}).get('entity') or {})

    order_notes = order_entity.get('notes') or {}
    payment_notes = payment_entity.get('notes') or {}
    payment_link_notes = payment_link_entity.get('notes') or {}

    local_order_id = (
        str(
            order_notes.get('local_order_id')
            or payment_notes.get('local_order_id')
            or payment_link_notes.get('local_order_id')
            or ''
        ).strip()
        or _extract_order_id_from_reference_id(payment_link_entity.get('reference_id') or '')
    )

    razorpay_order_id = (
        str(order_entity.get('id') or '').strip()
        or str(payment_entity.get('order_id') or '').strip()
        or str(payment_link_entity.get('order_id') or '').strip()
    )
    payment_link_id = str(payment_link_entity.get('id') or '').strip()

    order_queryset = Order.objects.select_for_update().prefetch_related('items__product')
    order = None

    if local_order_id:
        order = order_queryset.filter(order_id=local_order_id).first()

    if order is None and razorpay_order_id:
        order = order_queryset.filter(razorpay_order_id=razorpay_order_id).first()

    if order is None and payment_link_id:
        order = order_queryset.filter(razorpay_order_id=payment_link_id).first()

    return order, payment_entity, order_entity, payment_link_entity


def _get_category_image_path(category):
    fallback_by_category = {
        'mens': 'images/menstshirts.webp',
        'men': 'images/menstshirts.webp',
        'womens': 'images/women.webp',
        'women': 'images/women.webp',
        'kids': 'images/kids.webp',
    }

    if category.image:
        normalized = category.image.replace('\\', '/').lstrip('/')
        resolved = _asset_exists(normalized)
        if resolved:
            return resolved

    fallback = fallback_by_category.get(category.category_name.lower(), 'images/homebg.svg')
    return _asset_exists(fallback) or fallback


def _resolve_static_image_path(raw_path, fallback):
    if raw_path:
        normalized = raw_path.replace('\\', '/').lstrip('/')
        resolved = _asset_exists(normalized)
        if resolved:
            return resolved
    return _asset_exists(fallback) or fallback


def _load_category_cards(queryset, fallback_map, empty_fallback):
    try:
        items = queryset
        results = []
        for item in items:
            key = (item.category_name or '').lower()
            results.append({
                'name': item.category_name,
                'image_path': _resolve_static_image_path(item.image, fallback_map.get(key, empty_fallback)),
                'description': item.description or 'Explore this category.',
                'page_url': item.page_url or '#',
            })
        return results
    except (ProgrammingError, OperationalError):
        return []


def _get_fashion_card_fallback(category):
    fallback_map = {
        't-shirts': 'images/shirt.webp',
        'shirts': 'images/shirt.webp',
        'polos': 'images/menstshirts.webp',
        'jeans': 'images/cargo-pants.webp',
        'trousers': 'images/cargo-pants.webp',
        'cargo pants': 'images/cargo-pants.webp',
        'shorts': 'images/menstshirts.webp',
        'hoodies & sweatshirts': 'images/menstshirts.webp',
        'jackets & coats': 'images/menstshirts.webp',
        'blazers': 'images/menstshirts.webp',
        'suits': 'images/menstshirts.webp',
        'innerwear': 'images/menstshirts.webp',
        'nightwear': 'images/menstshirts.webp',
        'activewear': 'images/menstshirts.webp',
        'traditional wear': 'images/shirt.webp',
        'footwear': 'images/mens_shoes.webp',
        'accessories': 'images/mens_accessories.webp',
        'ethnic wear': 'images/women.webp',
        'western wear': 'images/women.webp',
        'lingerie & sleepwear': 'images/women.webp',
        'maternity wear': 'images/women.webp',
        'winter wear': 'images/women.webp',
        'bags & accessories': 'images/women.webp',
        'boys wear': 'images/kids.webp',
        'girls wear': 'images/kids.webp',
        'boys': 'images/kids.webp',
        'girls': 'images/kids.webp',
        'baby care': 'images/kids.webp',
        'toys': 'images/kids.webp',
    }
    root_name = (getattr(category.root_category, 'category_name', '') or '').lower()
    root_fallbacks = {
        'mens': 'images/menstshirts.webp',
        'womens': 'images/women.webp',
        'kids': 'images/kids.webp',
        'accessories': 'images/mens_accessories.webp',
    }
    name = (category.name or '').lower()
    return fallback_map.get(name, root_fallbacks.get(root_name, 'images/homebg.svg'))


def _public_fashion_category_filter():
    return Q(is_active=True) | Q(is_under_maintenance=True)


def _is_maintenance_category(category):
    current = category
    while current:
        if current.is_under_maintenance:
            return True
        current = current.parent
    return False


def _preferred_root_category(category_name):
    """Choose one usable hierarchy when imports contain roots with both cases.

    Fresh databases are seeded with lower-case names (``mens``/``womens``),
    while older imports can also contain title-case roots. Prefer the title-case
    hierarchy when it has public nodes, but retain the fresh-database fallback.
    """
    normalized = (category_name or '').strip().lower()
    if not normalized:
        return None

    canonical_name = normalized.capitalize()
    candidates = list(
        Category.objects.filter(category_name__iexact=normalized).order_by('category_id')
    )
    candidates.sort(
        key=lambda category: (
            category.category_name != canonical_name,
            category.category_name != normalized,
            category.category_id,
        )
    )
    for category in candidates:
        if FashionCategory.objects.filter(
            _public_fashion_category_filter(),
            root_category=category,
            parent__isnull=True,
        ).exists():
            return category
    return candidates[0] if candidates else None


def _get_maintenance_return_url(fashion_category):
    root_slug = _fashion_root_slug(fashion_category)
    if fashion_category.parent_id and fashion_category.parent.parent_id:
        return _get_fashion_category_url(fashion_category.parent)
    if root_slug in {'mens', 'womens', 'kids'}:
        return f'/{root_slug}/'
    return '/'


def _render_fashion_maintenance(request, fashion_category):
    category_return_url = _get_maintenance_return_url(fashion_category)
    return render(request, 'store/under-maintenance.html', {
        'page_title': fashion_category.name,
        'status_message': f'{fashion_category.name} is currently under maintenance. Please check back later.',
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'category_tree': _build_category_tree(),
        'active_fashion_category': fashion_category,
        'category_return_url': category_return_url,
    })


def _fashion_category_path(category):
    slugs = []
    current = category
    while current and current.parent_id:
        slugs.append(current.slug)
        current = current.parent
    return '/'.join(reversed(slugs))


def _fashion_root_slug(category):
    root_name = (getattr(category.root_category, 'category_name', '') or '').lower()
    return {
        'mens': 'mens',
        'womens': 'womens',
        'kids': 'kids',
        'accessories': 'accessories',
    }.get(root_name, root_name)

def _get_fashion_category_url(category):
    root_slug = _fashion_root_slug(category)
    category_path = _fashion_category_path(category)
    if root_slug == 'accessories':
        return reverse('shared_accessories')
    if category_path:
        return f'/{root_slug}/{category_path}/'
    return reverse('category', kwargs={'category_name': root_slug})

def _get_product_url(product):
    if product.fashion_category_id and product.fashion_category:
        root_slug = _fashion_root_slug(product.fashion_category)
        category_path = _fashion_category_path(product.fashion_category)

        if root_slug in {'mens', 'womens', 'kids'} and category_path and product.slug:
            return f'/{root_slug}/{category_path}/{product.slug}/'

    return reverse(
        'catalog_product_detail',
        kwargs={'product_id': product.product_id}
    )

def _build_fashion_category_cards(root_category):
    try:
        root_node = FashionCategory.objects.filter(
            _public_fashion_category_filter(),
            root_category=root_category,
            parent__isnull=True,
        ).order_by('sort_order', 'fashion_category_id').first()
        if not root_node:
            return []

        cards = []
        for item in root_node.children.filter(_public_fashion_category_filter()).order_by('sort_order', 'fashion_category_id'):
            cards.append({
                'id': item.fashion_category_id,
                'name': item.name,
                'image_path': _resolve_static_image_path(item.image, _get_fashion_card_fallback(item)),
                'description': item.description or f'Explore {item.name.lower()} styles and essentials.',
                # Build the route from the stored hierarchy.  Imported
                # ``page_url`` values can contain obsolete slugs and would
                # otherwise surface as customer-facing 404 links.
                'page_url': _get_fashion_category_url(item),
                'is_under_maintenance': _is_maintenance_category(item),
            })
        return cards
    except (ProgrammingError, OperationalError):
        return []
def _build_fashion_child_cards(parent_category):
    cards = []

    for item in parent_category.children.filter(
        _public_fashion_category_filter()
    ).order_by("sort_order", "fashion_category_id"):
        cards.append({
            "id": item.fashion_category_id,
            "name": item.name,
            "image_path": _resolve_static_image_path(item.image, _get_fashion_card_fallback(item)),
            "description": item.description or f"Explore {item.name.lower()} in {parent_category.name.lower()}.",
            "page_url": _get_fashion_category_url(item),
            "is_under_maintenance": _is_maintenance_category(item),
        })

    return cards


def _build_product_cards(products):
    image_cards = []

    for product in products:
        variant = _get_first_variant(product)
        image_names = [variant.image1, variant.image2, variant.image3, variant.image4] if variant else []

        valid_images = []
        for image_name in image_names:
            if not image_name:
                continue
            resolved = _asset_exists(f'images/{image_name}')
            if resolved:
                valid_images.append(resolved)

        if not valid_images:
            valid_images = [_get_product_image_path(product, variant)]

        primary_image = valid_images[0] if valid_images else _get_product_image_path(product, variant)
        image_cards.append({
            'product_id': product.product_id,
            'product_url': _get_product_url(product),
            'product_name': product.product_name,
            'brand': product.brand,
            'offer_price': product.offer_price,
            'image_path': primary_image,
            'card_name': product.product_name,
        })

    return image_cards


def _get_first_variant(product):
    prefetched_variants = getattr(product, '_prefetched_objects_cache', {}).get('productvariant_set')
    if prefetched_variants is not None:
        return prefetched_variants[0] if prefetched_variants else None
    return ProductVariant.objects.filter(product=product).first()


def _get_product_variants(product):
    prefetched_variants = getattr(product, '_prefetched_objects_cache', {}).get('productvariant_set')
    if prefetched_variants is not None:
        return list(prefetched_variants)
    return list(ProductVariant.objects.filter(product=product))


def _safe_redirect_target(request, raw_target, fallback='home'):
    fallback_url = reverse(fallback) if isinstance(fallback, str) and not fallback.startswith('/') else fallback
    target = (raw_target or '').strip()
    if not target:
        return fallback_url
    if url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return target
    return fallback_url


def _parse_quantity(raw_value, default=1):
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return default


def _build_category_tree():
    try:
        preferred_root_ids = [
            category.category_id
            for category in (
                _preferred_root_category(root_name)
                for root_name in ('mens', 'womens', 'kids', 'accessories')
            )
            if category is not None
        ]
        categories = list(
            FashionCategory.objects.filter(
                _public_fashion_category_filter(),
                root_category_id__in=preferred_root_ids,
            )
            .select_related('parent', 'root_category')
            .order_by('level', 'sort_order', 'fashion_category_id')
        )
    except (ProgrammingError, OperationalError):
        return []

    children_by_parent = {}
    for category in categories:
        children_by_parent.setdefault(category.parent_id, []).append(category)

    def serialize(category):
        return {
            'category': category,
            'children': [serialize(child) for child in children_by_parent.get(category.fashion_category_id, [])],
        }

    return [serialize(category) for category in children_by_parent.get(None, [])]



def _catalog_context(request, products, page_title, search_placeholder_text='Search fashion...'):
    products = products.order_by('-product_id').distinct()

    return {
        'page_title': page_title,
        'image_cards': _build_product_cards(products),
        'products_count': products.count(),
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'page_background_image': _get_site_asset('home_background', 'images/homebg.svg'),
        'page_background_url': _get_site_asset_url('home_background', 'images/homebg.svg'),
        'detail_url_name': 'catalog_product_detail',
    }


def _listing_page_title(fashion_category):
    if fashion_category.parent_id and fashion_category.parent.parent_id:
        main_name = fashion_category.parent.name.replace(' Wear', '')
        return f'All {main_name} {fashion_category.name}'
    return fashion_category.name


def store_asset(request, asset_path):
    normalized = Path((asset_path or '').replace('\\', '/').lstrip('/'))

    if normalized.is_absolute() or '..' in normalized.parts or normalized.parts[:1] not in {('images',), ('uploads',), (DB_UPLOAD_PREFIX,)}:
        raise Http404('Asset not found.')

    resolved = _asset_exists(normalized.as_posix())
    if not resolved:
        raise Http404('Asset not found.')

    resolved_path = Path(resolved)
    if resolved_path.parts[:1] == (DB_UPLOAD_PREFIX,):
        image = UploadedImage.objects.filter(path=resolved_path.as_posix()).first()
        if image is None:
            raise Http404('Asset not found.')
        response = HttpResponse(bytes(image.data), content_type=image.content_type or 'application/octet-stream')
        response['Content-Length'] = str(image.size or len(image.data))
        return _set_asset_cache_headers(response, UPLOAD_ASSET_MAX_AGE)

    if resolved_path.parts[:1] == ('uploads',):
        file_path = Path(settings.MEDIA_ROOT) / resolved_path
        content_type, _ = mimetypes.guess_type(file_path.name)
        response = FileResponse(file_path.open('rb'), content_type=content_type or 'application/octet-stream')
        return _set_asset_cache_headers(response, UPLOAD_ASSET_MAX_AGE)

    file_path = Path(settings.BASE_DIR) / 'static' / resolved_path
    content_type, _ = mimetypes.guess_type(file_path.name)
    response = FileResponse(file_path.open('rb'), content_type=content_type or 'application/octet-stream')
    return _set_asset_cache_headers(response, STATIC_ASSET_MAX_AGE)

def _get_session_cart(request):
    session_cart = request.session.get('cart', {})
    normalized_cart = {}

    if not isinstance(session_cart, dict):
        return normalized_cart

    for raw_key, raw_value in session_cart.items():
        if isinstance(raw_value, dict):
            product_id = raw_value.get('product_id')
            selected_size = (raw_value.get('selected_size') or '').strip() or None
            quantity = raw_value.get('quantity', 1)
        else:
            product_id = raw_key
            selected_size = None
            quantity = raw_value

        try:
            product_id = int(product_id)
            quantity = max(1, int(quantity))
        except (TypeError, ValueError):
            continue

        item_key = _build_session_cart_item_key(product_id, selected_size)
        normalized_cart[item_key] = {
            'product_id': product_id,
            'selected_size': selected_size,
            'quantity': quantity,
        }

    return normalized_cart


def _set_session_cart(request, cart_data):
    normalized_cart = {}
    for item_key, item_data in (cart_data or {}).items():
        if not isinstance(item_data, dict):
            continue

        try:
            product_id = int(item_data.get('product_id'))
            quantity = int(item_data.get('quantity', 0))
        except (TypeError, ValueError):
            continue

        if quantity <= 0:
            continue

        selected_size = (item_data.get('selected_size') or '').strip() or None
        normalized_key = _build_session_cart_item_key(product_id, selected_size)
        normalized_cart[normalized_key] = {
            'product_id': product_id,
            'selected_size': selected_size,
            'quantity': quantity,
        }

    request.session['cart'] = normalized_cart
    request.session.modified = True


def _build_session_cart_item_key(product_id, selected_size=None):
    normalized_size = (selected_size or '').strip()
    return f'{int(product_id)}::{normalized_size.lower()}'


def _add_to_session_cart(request, product, quantity, selected_size=None, replace=False):
    quantity = max(1, int(quantity))
    if selected_size:
        matching_variant = ProductVariant.objects.filter(product=product, size__iexact=selected_size).first()
        available_stock = max(0, getattr(matching_variant, 'quantity', 0) or 0)
    else:
        available_stock = sum(ProductVariant.objects.filter(product=product).values_list('quantity', flat=True))

    if available_stock == 0:
        return False, 0

    session_cart = _get_session_cart(request)
    item_key = _build_session_cart_item_key(product.product_id, selected_size)
    current_item = session_cart.get(item_key, {
        'product_id': product.product_id,
        'selected_size': selected_size,
        'quantity': 0,
    })
    current_quantity = int(current_item.get('quantity', 0))
    final_quantity = quantity if replace else current_quantity + quantity
    final_quantity = min(final_quantity, available_stock)

    session_cart[item_key] = {
        'product_id': product.product_id,
        'selected_size': selected_size,
        'quantity': final_quantity,
    }
    _set_session_cart(request, session_cart)
    return True, available_stock


def _remove_session_cart_item(request, item_key):
    session_cart = _get_session_cart(request)
    if item_key in session_cart:
        session_cart.pop(item_key, None)
        _set_session_cart(request, session_cart)


def _merge_session_cart_into_db(request, user):
    session_cart = _get_session_cart(request)
    if not session_cart:
        return

    for item_data in session_cart.values():
        product = Product.objects.filter(product_id=item_data['product_id'], is_active=True).first()
        if not product:
            continue

        cart_item, created = CartItem.objects.get_or_create(
            user=user,
            product=product,
            selected_size=item_data['selected_size'],
            defaults={'quantity': max(1, int(item_data['quantity']))},
        )
        if not created:
            cart_item.quantity += max(1, int(item_data['quantity']))
            cart_item.save(update_fields=['quantity', 'updated_at'])

    _set_session_cart(request, {})


def _get_user_cart_items(user):
    return (
        CartItem.objects.filter(user=user, product__is_active=True)
        .select_related('product', 'product__category', 'product__subcategory')
        .prefetch_related('product__productvariant_set')
    )


def _get_effective_cart_data(request):
    if request.user.is_authenticated:
        return _get_user_cart_items(request.user)
    return _get_session_cart(request)


def _set_user_cart_item(user, product, quantity, selected_size=None, replace=False):
    quantity = max(1, int(quantity))
    if selected_size:
        matching_variant = ProductVariant.objects.filter(product=product, size__iexact=selected_size).first()
        available_stock = max(0, getattr(matching_variant, 'quantity', 0) or 0)
    else:
        available_stock = sum(ProductVariant.objects.filter(product=product).values_list('quantity', flat=True))

    if available_stock == 0:
        return False, 0

    cart_item, created = CartItem.objects.get_or_create(
        user=user,
        product=product,
        selected_size=selected_size,
        defaults={'quantity': quantity},
    )
    if not created:
        cart_item.quantity = quantity if replace else cart_item.quantity + quantity
        cart_item.quantity = min(cart_item.quantity, available_stock)
        cart_item.save(update_fields=['quantity', 'updated_at'])
    else:
        if cart_item.quantity > available_stock:
            cart_item.quantity = available_stock
            cart_item.save(update_fields=['quantity', 'updated_at'])

    return True, available_stock


def _remove_user_cart_item(user, product_id):
    CartItem.objects.filter(user=user, product_id=product_id).delete()


def _build_cart_summary(cart_data):
    cart_items = []
    subtotal = 0
    total_items = 0

    if hasattr(cart_data, 'model') and cart_data.model is CartItem:
        for item in cart_data:
            product = item.product
            variants = _get_product_variants(product)
            variant = next(
                (
                    candidate for candidate in variants
                    if item.selected_size and (candidate.size or '').lower() == item.selected_size.lower()
                ),
                None,
            )
            if variant is None:
                variant = variants[0] if variants else None
            line_total = product.offer_price * item.quantity
            subtotal += line_total
            total_items += item.quantity
            cart_items.append({
                'cart_item_id': item.cart_item_id,
                'session_item_key': None,
                'product': product,
                'variant': variant,
                'selected_size': item.selected_size,
                'quantity': item.quantity,
                'image_path': _get_product_image_path(product, variant),
                'line_total': line_total,
            })
    else:
        product_ids = [
            item_data['product_id']
            for item_data in cart_data.values()
            if isinstance(item_data, dict) and item_data.get('product_id')
        ]
        products_by_id = {
            product.product_id: product
            for product in Product.objects.filter(product_id__in=product_ids, is_active=True)
            .select_related('category', 'subcategory').prefetch_related('productvariant_set')
            .prefetch_related('productvariant_set')
        }
        for item_key, item_data in cart_data.items():
            product = products_by_id.get(item_data['product_id'])
            if not product:
                continue

            selected_size = item_data.get('selected_size')
            quantity = int(item_data.get('quantity', 0))
            variants = _get_product_variants(product)
            variant = next(
                (
                    candidate for candidate in variants
                    if selected_size and (candidate.size or '').lower() == selected_size.lower()
                ),
                None,
            )
            if variant is None:
                variant = variants[0] if variants else None
            line_total = product.offer_price * quantity
            subtotal += line_total
            total_items += quantity
            cart_items.append({
                'cart_item_id': None,
                'session_item_key': item_key,
                'product': product,
                'variant': variant,
                'selected_size': selected_size,
                'quantity': quantity,
                'image_path': _get_product_image_path(product, variant),
                'line_total': line_total,
            })

    return cart_items, subtotal, total_items

# Home page

def home(request):
    categories = Category.objects.order_by('category_id')
    categories_context = []
    home_content = None
    products = (
        Product.objects.filter(is_active=True)
        .select_related('category', 'subcategory', 'fashion_category')
        .prefetch_related('productvariant_set')
        .order_by('-is_featured', '-product_id')[:12]
    )

    try:
        home_content = HomeContent.objects.filter(is_active=True).first()
    except (ProgrammingError, OperationalError):
        home_content = None

    categories_context = [
    {
        'name': 'Men',
        'image_path': 'images/menstshirts.webp',
        'route_name': 'mens_root',
        'description': 'Casual wear, formals, accessories and more.',
    },
    {
        'name': 'Women',
        'image_path': 'images/women.webp',
        'route_name': 'womens_root',
        'description': 'Ethnic, western, and everything chic!',
    },
    {
        'name': 'Kids',
        'image_path': 'images/kids.webp',
        'route_name': 'kids_root',
        'description': 'Trendy and comfy styles for kids of all ages.',
    },
]

    logo_image = _get_site_asset('logo', 'images/logo1.webp')
    background_image = _get_site_asset('home_background', 'images/homebg.svg')
    return render(request, 'store/home.html', {
        'products': products,
        'categories_context': categories_context,
        'logo_image': logo_image,
        'background_image': background_image,
        'background_url': _asset_public_url(background_image),
        'hero_subtitle': getattr(home_content, 'hero_subtitle', None) or 'For Modern Shoppers',
        'hero_title': getattr(home_content, 'hero_title', None) or 'Relax,',
        'hero_highlight': getattr(home_content, 'hero_highlight', None) or 'ShopEase',
        'hero_tagline': getattr(home_content, 'hero_tagline', None) or 'Faster, Fairer, and Closer to You',
        'hero_button_text': getattr(home_content, 'hero_button_text', None) or 'Explore Products',
        'hero_button_url': getattr(home_content, 'hero_button_url', None) or '/men/',
        'search_placeholder': getattr(home_content, 'search_placeholder', None) or 'Search for products...',
        'search_button_text': getattr(home_content, 'search_button_text', None) or 'Search',
        'nav_home_text': getattr(home_content, 'nav_home_text', None) or 'Home',
        'nav_home_url': getattr(home_content, 'nav_home_url', None) or '/',
        'nav_products_text': getattr(home_content, 'nav_products_text', None) or 'Products',
        'nav_products_url': getattr(home_content, 'nav_products_url', None) or '#categories-section',
        'nav_contact_text': getattr(home_content, 'nav_contact_text', None) or 'Contact Us',
        'nav_contact_url': reverse('contact_html'),
        'nav_login_text': getattr(home_content, 'nav_login_text', None) or 'Login',
        'nav_login_url': getattr(home_content, 'nav_login_url', None) or '/login/',
        'category_section_title': getattr(home_content, 'category_section_title', None) or 'Shop by Category',
        'footer_text': getattr(home_content, 'footer_text', None) or 'All rights reserved.',
        'footer_brand_text': getattr(home_content, 'footer_brand_text', None) or 'ShopEase',
        'footer_builder_text': getattr(home_content, 'footer_builder_text', None) or 'Varshak Shopeasy',
        'search_query': '',       
   })

def search_results(request):
    search_query = (request.GET.get('q') or '').strip()
    products = Product.objects.none()
    if search_query:
        search_tokens = [token for token in search_query.replace('-', ' ').split() if token]
        search_filter = (
            Q(product_name__icontains=search_query) |
            Q(brand__icontains=search_query) |
            Q(category__category_name__icontains=search_query) |
            Q(subcategory__subcategory_name__icontains=search_query)
        )

        for token in search_tokens:
            search_filter |= (
                Q(product_name__icontains=token) |
                Q(brand__icontains=token) |
                Q(category__category_name__icontains=token) |
                Q(subcategory__subcategory_name__icontains=token)
            )

        if 'tshirt' in search_query.lower() or 't shirt' in search_query.lower() or 't-shirt' in search_query.lower():
            search_filter |= (
                Q(product_name__icontains='t-shirt') |
                Q(product_name__icontains='t shirt') |
                Q(product_name__icontains='tshirt') |
                Q(subcategory__subcategory_name__icontains='t-shirt') |
                Q(subcategory__subcategory_name__icontains='t shirt') |
                Q(subcategory__subcategory_name__icontains='tshirt')
            )

        products = (
            Product.objects.filter(search_filter, is_active=True)
            .select_related('category', 'subcategory', 'fashion_category')
            .prefetch_related('productvariant_set')
            .distinct()
            .order_by('-product_id')
        )

    searched_products = []
    for product in products:
        variant = _get_first_variant(product)
        searched_products.append({
            'product': product,
            'image_path': _get_product_image_path(product, variant),
        })

    try:
        home_content = HomeContent.objects.filter(is_active=True).first()
    except (ProgrammingError, OperationalError):
        home_content = None

    return render(request, 'store/search-results.html', {
        'searched_products': searched_products,
        'search_query': search_query,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'search_placeholder': getattr(home_content, 'search_placeholder', None) or 'Search for products...',
        'search_button_text': getattr(home_content, 'search_button_text', None) or 'Search',
        'nav_home_text': getattr(home_content, 'nav_home_text', None) or 'Home',
        'nav_home_url': getattr(home_content, 'nav_home_url', None) or '/',
        'nav_products_text': getattr(home_content, 'nav_products_text', None) or 'Products',
        'nav_products_url': getattr(home_content, 'nav_products_url', None) or '#categories-section',
        'nav_contact_text': getattr(home_content, 'nav_contact_text', None) or 'Contact Us',
        'nav_contact_url': reverse('contact_html'),
        'nav_login_text': getattr(home_content, 'nav_login_text', None) or 'Login',
        'nav_login_url': getattr(home_content, 'nav_login_url', None) or '/login/',
        'footer_text': getattr(home_content, 'footer_text', None) or 'All rights reserved.',
        'footer_brand_text': getattr(home_content, 'footer_brand_text', None) or 'ShopEase',
        'footer_builder_text': getattr(home_content, 'footer_builder_text', None) or 'Varshak Shopeasy',
    })


def contact_us(request):
    return render(request, 'store/contact-us.html', _shop_contact_context())


def _shop_contact_context():
    return {
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'phone_number': '8247624897',
        'email_address': 'saivarshak14@gmail.com',
    }


def _policy_page_context(title, summary, sections):
    context = _shop_contact_context()
    context.update({
        'page_title': title,
        'page_summary': summary,
        'updated_on': 'April 23, 2026',
        'sections': sections,
    })
    return context


def privacy_policy(request):
    return render(request, 'store/policy-page.html', _policy_page_context(
        title='Privacy Policy',
        summary='This page explains what customer information ShopEase collects and how it is used to process orders and support customers.',
        sections=[
            {
                'heading': 'Information We Collect',
                'points': [
                    'We collect the details you submit during account registration, checkout, and customer support conversations.',
                    'This can include your name, email address, phone number, delivery address, and order information.',
                ],
            },
            {
                'heading': 'How We Use Your Information',
                'points': [
                    'We use this information to process your orders, confirm payments, arrange delivery, and respond to support requests.',
                    'We may also use your contact details for order updates, invoices, refund communication, and important service notifications.',
                ],
            },
            {
                'heading': 'Payments and Security',
                'points': [
                    'Online payments are handled through secure third-party payment providers such as Razorpay.',
                    'ShopEase does not store your full card or UPI credentials on this website.',
                ],
            },
            {
                'heading': 'Sharing of Information',
                'points': [
                    'Customer information is shared only with service providers needed to complete payment, delivery, and support activities.',
                    'We do not sell personal information to unrelated third parties.',
                ],
            },
        ],
    ))


def refund_policy(request):
    return render(request, 'store/policy-page.html', _policy_page_context(
        title='Refund and Cancellation Policy',
        summary='This page describes how ShopEase handles order cancellation, return review, and approved refunds.',
        sections=[
            {
                'heading': 'Order Cancellation',
                'points': [
                    'Customers can request cancellation before the order is dispatched by contacting ShopEase support.',
                    'Once an order has shipped, cancellation may no longer be available and the request may be handled as a return or refund review.',
                ],
            },
            {
                'heading': 'Return and Refund Review',
                'points': [
                    'If you receive a damaged, defective, or incorrect item, contact ShopEase with your order details and issue description.',
                    'Eligible refund or replacement requests are reviewed after the support team verifies the order and reported issue.',
                ],
            },
            {
                'heading': 'Refund Processing',
                'points': [
                    'Approved refunds are sent back to the original payment method used during checkout.',
                    'The final credit timeline depends on the payment provider and your bank after ShopEase completes the refund.',
                ],
            },
            {
                'heading': 'Support for Refund Requests',
                'points': [
                    'For any cancellation, refund, or return question, customers should contact ShopEase through the contact details listed on the website.',
                ],
            },
        ],
    ))


def terms_and_conditions(request):
    return render(request, 'store/policy-page.html', _policy_page_context(
        title='Terms and Conditions',
        summary='These terms govern the use of the ShopEase website, order placement, payments, and customer responsibilities.',
        sections=[
            {
                'heading': 'Orders and Availability',
                'points': [
                    'All orders are subject to product availability, payment confirmation, and acceptance by ShopEase.',
                    'If a product becomes unavailable after an order is placed, ShopEase may contact the customer to offer an update, replacement, or refund.',
                ],
            },
            {
                'heading': 'Pricing and Payments',
                'points': [
                    'Product prices displayed on the website are shown in the stated currency and may change without prior notice.',
                    'Orders are processed only after successful payment authorization or confirmation through the selected payment method.',
                ],
            },
            {
                'heading': 'Customer Responsibilities',
                'points': [
                    'Customers must provide accurate billing, shipping, and contact details when placing an order.',
                    'ShopEase may delay or cancel orders that appear fraudulent, incomplete, or inconsistent with payment verification requirements.',
                ],
            },
            {
                'heading': 'Website Use',
                'points': [
                    'Users agree not to misuse the website, interfere with store operations, or submit false information through forms or checkout.',
                    'Continued use of the ShopEase website means acceptance of the latest published terms and policies.',
                ],
            },
        ],
    ))

def category_view(request, category_name):
    category_lookup = {
        "men": "mens",
        "women": "womens",
        "kids": "kids",
    }

    canonical_root_urls = {
        "men": "/mens/",
        "mens": "/mens/",
        "women": "/womens/",
        "womens": "/womens/",
        "kids": "/kids/",
    }

    requested_key = (category_name or "").lower()

    if request.path.startswith("/category") and requested_key in canonical_root_urls:
        return redirect(canonical_root_urls[requested_key], permanent=True)

    template_lookup = {
        "mens": "store/category-men.html",
        "womens": "store/category-women.html",
        "kids": "store/category-kids.html",
    }


    normalized = category_lookup.get(requested_key, requested_key)

    # Category primary keys are database-dependent. Historical imports may
    # contain a legacy lower-case root alongside the current title-case root.
    category = _preferred_root_category(normalized)
    if category is None:
        category = get_object_or_404(Category, category_name__iexact=normalized)

    products = (
        Product.objects.filter(category=category, is_active=True)
        .select_related("category", "subcategory")
        .prefetch_related("productvariant_set")
    )

    template_name = template_lookup.get(
        category.category_name.lower(),
        "store/category-men.html",
    )

    context = {
        "products": products,
        "category": category,
    }

    root = FashionCategory.objects.filter(
        _public_fashion_category_filter(),
        root_category=category,
        parent__isnull=True,
    ).order_by('sort_order', 'fashion_category_id').first()

    fashion_categories = (
        FashionCategory.objects.filter(
            parent=root,
            is_active=True,
        ).order_by("sort_order")
        if root
        else FashionCategory.objects.none()
    )

    if category.category_name.lower() == "mens":
        fallback_map = {
            "t-shirts": "images/shirt.webp",
            "shirts": "images/shirt.webp",
            "jeans": "images/cargo-pants.webp",
            "accessories": "images/mens_accessories.webp",
        }

        context["men_categories"] = (
            _build_fashion_category_cards(category)
            or _load_category_cards(
                fashion_categories,
                fallback_map,
                "images/shirt.webp",
            )
        )

        context["page_background_image"] = _get_page_asset(
            "category_men",
            "background",
            "images/menstshirts.webp",
        )

    elif category.category_name.lower() == "womens":
        fallback_map = {
            "ethnic wear": "images/women.webp",
            "western wear": "images/women.webp",
            "footwear": "images/women.webp",
            "bags & accessories": "images/women.webp",
            "bags and accessories": "images/women.webp",
        }

        context["women_categories"] = (
            _build_fashion_category_cards(category)
            or _load_category_cards(
                fashion_categories,
                fallback_map,
                "images/women.webp",
            )
        )

        context["page_background_image"] = _get_page_asset(
            "category_women",
            "background",
            "images/women.webp",
        )

    elif category.category_name.lower() == "kids":
        fallback_map = {
            "t-shirts": "images/kids.webp",
            "kids dresses": "images/kids.webp",
            "dresses": "images/kids.webp",
            "toys": "images/kids.webp",
            "footwear": "images/kids.webp",
        }

        context["kids_categories"] = (
            _build_fashion_category_cards(category)
            or _load_category_cards(
                fashion_categories,
                fallback_map,
                "images/kids.webp",
            )
        )

        context["page_background_image"] = _get_page_asset(
            "category_kids",
            "background",
            "images/kids.webp",
        )

    context["page_background_url"] = _asset_public_url(
        context["page_background_image"]
    )

    context["logo_image"] = _get_site_asset(
        "logo",
        "images/logo1.webp",
    )

    return render(request, template_name, context)


def fashion_catalog(request):
    products = (
        Product.objects.filter(is_active=True)
        .select_related('category', 'subcategory', 'fashion_category')
        .prefetch_related('productvariant_set')
    )
    context = _catalog_context(request, products, 'Fashion Catalog', 'Search fashion products...')
    context['category_tree'] = _build_category_tree()
    return render(request, 'store/product-listing-generic.html', context)


def fashion_category_listing(request, category_id):
    fashion_category = get_object_or_404(FashionCategory, fashion_category_id=category_id, is_active=True)
    return redirect(_get_fashion_category_url(fashion_category), permanent=True)


def _get_descendant_category_ids(fashion_category):
    descendant_ids = [fashion_category.fashion_category_id]
    children = list(fashion_category.children.filter(is_active=True))
    while children:
        child = children.pop()
        descendant_ids.append(child.fashion_category_id)
        children.extend(list(child.children.filter(is_active=True)))
    return descendant_ids


def _resolve_fashion_category_by_path(gender_slug, category_path):
    root_lookup = {
    'men': 'mens',       # optional legacy support
    'mens': 'mens',
    'women': 'womens',   # optional legacy support
    'womens': 'womens',
    'kids': 'kids',
    'accessories': 'accessories',
}
    root_category_name = root_lookup.get((gender_slug or '').lower())
    if not root_category_name:
        raise Http404('Category not found.')

    root_category = _preferred_root_category(root_category_name)
    if root_category is None:
        raise Http404('Category not found.')
    current = FashionCategory.objects.filter(
        _public_fashion_category_filter(),
        root_category=root_category,
        parent__isnull=True,
    ).order_by('sort_order', 'fashion_category_id').first()
    if not current:
        raise Http404('Category not found.')

    path_parts = [part for part in (category_path or '').strip('/').split('/') if part]
    for slug in path_parts:
        current = current.children.filter(_public_fashion_category_filter(), slug=slug).order_by('sort_order', 'fashion_category_id').first()
        if not current:
            raise Http404('Category not found.')
    return current


def fashion_category_slug_listing(request, gender_slug, category_path):
    fashion_category = _resolve_fashion_category_by_path(gender_slug, category_path)
    child_cards = _build_fashion_child_cards(fashion_category)

    canonical_url = _get_fashion_category_url(fashion_category)
    if request.path != canonical_url:
        return redirect(canonical_url, permanent=True)

    if _is_maintenance_category(fashion_category):
        return _render_fashion_maintenance(request, fashion_category)

    if child_cards:
        return render(request, 'store/category-children.html', {
            'page_title': fashion_category.name,
            'parent_category': fashion_category,
            'category_cards': child_cards,
            'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
            'page_background_image': _resolve_static_image_path(
                fashion_category.banner_image,
                _get_fashion_card_fallback(fashion_category)
            ),
            'page_background_url': _asset_public_url(
                _resolve_static_image_path(
                    fashion_category.banner_image,
                    _get_fashion_card_fallback(fashion_category)
                )
            ),
            'category_tree': _build_category_tree(),
        })

    descendant_ids = _get_descendant_category_ids(fashion_category)

    products = Product.objects.filter(
        Q(fashion_category_id__in=descendant_ids) |
        Q(
            category=fashion_category.root_category,
            subcategory__subcategory_name__iexact=fashion_category.name
        ),
        is_active=True,
    ).select_related(
        'category',
        'subcategory',
        'fashion_category',
        'fashion_category__parent',
        'fashion_category__root_category'
    ).prefetch_related('productvariant_set')

    context = _catalog_context(
        request,
        products,
        _listing_page_title(fashion_category),
        f'Search {fashion_category.name.lower()}...'
    )

    context['category_tree'] = _build_category_tree()
    context['active_fashion_category'] = fashion_category

    return render(request, 'store/product-listing-generic.html', context)

def fashion_product_slug_detail(request, gender_slug, category_path, product_slug):
    nested_category_path = f"{category_path.rstrip('/')}/{product_slug}"
    try:
        _resolve_fashion_category_by_path(gender_slug, nested_category_path)
    except Http404:
        pass
    else:
        return fashion_category_slug_listing(request, gender_slug, nested_category_path)

    fashion_category = _resolve_fashion_category_by_path(gender_slug, category_path)
    if _is_maintenance_category(fashion_category):
        return _render_fashion_maintenance(request, fashion_category)
    descendant_ids = _get_descendant_category_ids(fashion_category)
    product = get_object_or_404(
        Product.objects.select_related('category', 'subcategory', 'fashion_category', 'fashion_category__parent', 'fashion_category__root_category').prefetch_related('productvariant_set'),
        fashion_category_id__in=descendant_ids,
        slug=product_slug,
        is_active=True,
    )
    canonical_url = _get_product_url(product)
    if request.path != canonical_url:
        return redirect(canonical_url, permanent=True)
    return _render_product_detail(request, product)


def shared_accessories(request):
    return fashion_category_slug_listing(request, 'accessories', 'common-for-all-genders')
    return fashion_category_slug_listing(request, 'accessories', 'common-for-all-genders')


def _render_product_detail(request, product):
    if product.fashion_category_id and _is_maintenance_category(product.fashion_category):
        return _render_fashion_maintenance(request, product.fashion_category)
    variant = _get_first_variant(product)
    fallback_image = _get_product_image_path(product, variant)
    size_variants = list(ProductVariant.objects.filter(product=product).exclude(size__isnull=True).exclude(size__exact='').order_by('variant_id'))
    related_queryset = Product.objects.filter(category=product.category, is_active=True)
    if product.fashion_category_id:
        related_queryset = related_queryset.filter(fashion_category=product.fashion_category)
    related_products = related_queryset.exclude(product_id=product.product_id).select_related('category', 'subcategory', 'fashion_category', 'fashion_category__parent', 'fashion_category__root_category').prefetch_related('productvariant_set')[:4]
    return render(request, 'store/product-detail-generic.html', {
        'product': product,
        'variant': variant,
        'size_variants': size_variants,
        'related_products': _build_product_cards(related_products),
        'is_wishlisted': request.user.is_authenticated and WishlistItem.objects.filter(user=request.user, product=product).exists(),
        'total_stock': _get_total_stock(product),
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'detail_fallback_front': fallback_image,
        'detail_fallback_back': fallback_image,
        'detail_fallback_side': fallback_image,
        'detail_fallback_close': fallback_image,
    })


def product_detail(request, product_id):
    product = get_object_or_404(
        Product.objects.select_related('category', 'subcategory', 'fashion_category', 'fashion_category__parent', 'fashion_category__root_category').prefetch_related('productvariant_set'),
        product_id=product_id,
        is_active=True,
    )
    product_url = _get_product_url(product)
    if product_url != request.path:
        return redirect(product_url, permanent=True)
    return _render_product_detail(request, product)


def mens_tshirts(request):
    return redirect('/men/casual-wear/t-shirts/', permanent=True)
    products = Product.objects.filter(
        category__category_name__iexact='mens',
        subcategory__subcategory_name__iexact='t-shirts',
        is_active=True,
    ).select_related('category', 'subcategory').prefetch_related('productvariant_set')

    image_cards = []
    fallback_images = [
        _get_page_asset('mens_tshirt_detail', 'fallback_front', 'images/shirt.webp'),
        _get_page_asset('mens_tshirt_detail', 'fallback_side', 'images/T-shirt side.jpg'),
        _get_page_asset('mens_tshirt_detail', 'fallback_back', 'images/T-shirt back.jpg'),
        _get_page_asset('mens_tshirt_detail', 'fallback_close', 'images/T-shirt close.jpg'),
    ]

    for product in products:
        variant = _get_first_variant(product)
        image_names = [variant.image1, variant.image2, variant.image3, variant.image4] if variant else []

        valid_images = []
        for image_name in image_names:
            if not image_name:
                continue
            image_path = Path(settings.BASE_DIR) / 'static' / 'images' / image_name
            if image_path.exists():
                valid_images.append(f'images/{image_name}')

        if not valid_images:
            valid_images = [image_path for image_path in fallback_images if image_path]

        primary_image = valid_images[0] if valid_images else _get_product_image_path(product, variant)
        image_cards.append({
            'product_id': product.product_id,
            'product_name': product.product_name,
            'brand': product.brand,
            'offer_price': product.offer_price,
            'image_path': primary_image,
            'card_name': product.product_name,
        })

    page_background_image = _get_page_asset('mens_tshirts', 'background', 'images/menstshirts.jpg')
    return render(request, 'store/mens-tshirts.html', {
        'image_cards': image_cards,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'page_background_image': page_background_image,
        'page_background_url': _asset_public_url(page_background_image),
    })


def mens_tshirt_detail(request, product_id):
    product = get_object_or_404(
        Product.objects.select_related('category', 'subcategory').prefetch_related('productvariant_set'),
        product_id=product_id,
        category__category_name__iexact='mens',
        subcategory__subcategory_name__iexact='t-shirts',
        is_active=True,
    )
    return redirect(_get_product_url(product), permanent=True)


def mens_jeans(request):
    return redirect('/men/casual-wear/jeans/', permanent=True)
    products = Product.objects.filter(
        category__category_name__iexact='mens',
        subcategory__subcategory_name__iexact='jeans',
        is_active=True,
    ).select_related('category', 'subcategory').prefetch_related('productvariant_set')

    image_cards = []
    fallback_images = [
        _get_page_asset('mens_jeans_detail', 'fallback_front', 'images/cargo-pants.webp'),
        _get_page_asset('mens_jeans_detail', 'fallback_side', 'images/cargo-pants.webp'),
        _get_page_asset('mens_jeans_detail', 'fallback_back', 'images/cargo-pants.webp'),
        _get_page_asset('mens_jeans_detail', 'fallback_close', 'images/cargo-pants.webp'),
    ]

    for product in products:
        variant = _get_first_variant(product)
        image_names = [variant.image1, variant.image2, variant.image3, variant.image4] if variant else []

        valid_images = []
        for image_name in image_names:
            if not image_name:
                continue
            resolved = _asset_exists(f'images/{image_name}')
            if resolved:
                valid_images.append(resolved)

        if not valid_images:
            valid_images = [image_path for image_path in fallback_images if image_path]

        primary_image = valid_images[0] if valid_images else _get_product_image_path(product, variant)
        image_cards.append({
            'product_id': product.product_id,
            'product_name': product.product_name,
            'brand': product.brand,
            'offer_price': product.offer_price,
            'image_path': primary_image,
            'card_name': product.product_name,
        })

    page_background_image = _get_page_asset('mens_jeans', 'background', 'images/jeansbg.jpg')
    return render(request, 'store/mens-Jeans.html', {
        'image_cards': image_cards,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'page_background_image': page_background_image,
        'page_background_url': _asset_public_url(page_background_image),
    })


def mens_jeans_detail(request, product_id):
    product = get_object_or_404(
        Product.objects.select_related('category', 'subcategory').prefetch_related('productvariant_set'),
        product_id=product_id,
        category__category_name__iexact='mens',
        subcategory__subcategory_name__iexact='jeans',
        is_active=True,
    )
    return redirect(_get_product_url(product), permanent=True)


def accessories_men(request):
    return redirect('/men/accessories/', permanent=True)
    products = Product.objects.filter(
        category__category_name__iexact='mens',
        subcategory__subcategory_name__icontains='accessor',
        is_active=True,
    ).select_related('category', 'subcategory').prefetch_related('productvariant_set')

    return render(request, 'store/accessories-men.html', {
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'product_count': products.count(),
        'page_title': "Men's Accessories",
        'status_message': 'Our accessories section is currently under maintenance.',
    })


def accessories_women(request):
    return redirect('/women/accessories/', permanent=True)
    products = Product.objects.filter(
        category__category_name__iexact='womens',
        is_active=True,
    ).filter(
        Q(subcategory__subcategory_name__icontains='accessor') |
        Q(subcategory__subcategory_name__icontains='bag')
    ).select_related('category', 'subcategory').prefetch_related('productvariant_set')

    return render(request, 'store/accessories-women.html', {
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'product_count': products.count(),
        'page_title': "Women's Accessories",
        'status_message': 'Our accessories section is currently under maintenance.',
    })


def womens_ethnicware(request):
    return redirect('/women/casual-wear/', permanent=True)
    products = Product.objects.filter(
        category__category_name__iexact='womens',
        subcategory__subcategory_name__iexact='ethnic wear',
        is_active=True,
    ).select_related('category', 'subcategory').prefetch_related('productvariant_set')
    page_background_image = _get_page_asset('womens_ethnicware', 'background', 'images/ethicwearebg.jpeg')
    return render(request, 'store/product-listing-generic.html', {
        'page_title': 'Stylish Ethnic Wear for Women',
        'search_placeholder_text': 'Search ethnic wear...',
        'image_cards': _build_product_cards(products),
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'page_background_image': page_background_image,
        'page_background_url': _asset_public_url(page_background_image),
        'detail_url_name': 'catalog_product_detail',
    })


def womens_westernware(request):
    return redirect('/women/casual-wear/', permanent=True)
    products = Product.objects.filter(
        category__category_name__iexact='womens',
        subcategory__subcategory_name__iexact='western wear',
        is_active=True,
    ).select_related('category', 'subcategory').prefetch_related('productvariant_set')
    page_background_image = _get_page_asset('womens_westernware', 'background', 'images/westernware.jpg')
    return render(request, 'store/product-listing-generic.html', {
        'page_title': 'Stylish Western Wear for Women',
        'search_placeholder_text': 'Search western wear...',
        'image_cards': _build_product_cards(products),
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'page_background_image': page_background_image,
        'page_background_url': _asset_public_url(page_background_image),
        'detail_url_name': 'catalog_product_detail',
    })


def womens_footwear(request):
    return redirect('/women/footwear/', permanent=True)
    products = Product.objects.filter(
        category__category_name__iexact='womens',
        subcategory__subcategory_name__iexact='footwear',
        is_active=True,
    ).select_related('category', 'subcategory').prefetch_related('productvariant_set')
    page_background_image = _get_page_asset('womens_footwear', 'background', 'images/women.webp')
    return render(request, 'store/product-listing-generic.html', {
        'page_title': "Women's Footwear",
        'search_placeholder_text': 'Search footwear...',
        'image_cards': _build_product_cards(products),
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'page_background_image': page_background_image,
        'page_background_url': _asset_public_url(page_background_image),
        'detail_url_name': 'catalog_product_detail',
    })


def kids_tshirts(request):
    return redirect('/kids/casual-wear/t-shirts/', permanent=True)
    products = Product.objects.filter(
        category__category_name__iexact='kids',
        is_active=True,
    ).filter(
        Q(subcategory__subcategory_name__icontains='kids t-shirts') |
        Q(subcategory__subcategory_name__icontains='t-shirts') |
        Q(subcategory__subcategory_name__icontains='t shirts')
    ).select_related('category', 'subcategory').prefetch_related('productvariant_set')
    page_background_image = _get_page_asset('kids_tshirts', 'background', 'images/kidstshirtsbg.jpeg')
    return render(request, 'store/product-listing-generic.html', {
        'page_title': 'T-Shirts for Kids',
        'search_placeholder_text': 'Search kids t-shirts...',
        'image_cards': _build_product_cards(products),
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'page_background_image': page_background_image,
        'page_background_url': _asset_public_url(page_background_image),
        'detail_url_name': 'catalog_product_detail',
    })


def kids_dresses(request):
    
    products = Product.objects.filter(
        category__category_name__iexact='kids',
        is_active=True,
    ).filter(
        Q(subcategory__subcategory_name__icontains='kids dresses') |
        Q(subcategory__subcategory_name__icontains='dresses')
    ).select_related('category', 'subcategory').prefetch_related('productvariant_set')
    page_background_image = _get_page_asset('kids_dresses', 'background', 'images/kids.webp')
    return render(request, 'store/product-listing-generic.html', {
        'page_title': 'Kids Dresses',
        'search_placeholder_text': 'Search kids dresses...',
        'image_cards': _build_product_cards(products),
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'page_background_image': page_background_image,
        'page_background_url': _asset_public_url(page_background_image),
        'detail_url_name': 'catalog_product_detail',
    })


def kids_toys(request):
    return redirect('/kids/accessories/', permanent=True)
    products = Product.objects.filter(
        category__category_name__iexact='kids',
        subcategory__subcategory_name__iexact='toys',
        is_active=True,
    ).select_related('category', 'subcategory').prefetch_related('productvariant_set')
    page_background_image = _get_page_asset('kids_toys', 'background', 'images/kids.webp')
    return render(request, 'store/product-listing-generic.html', {
        'page_title': 'Kids Toys',
        'search_placeholder_text': 'Search kids toys...',
        'image_cards': _build_product_cards(products),
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'page_background_image': page_background_image,
        'page_background_url': _asset_public_url(page_background_image),
        'detail_url_name': 'catalog_product_detail',
    })


def kids_footwear(request):
    return redirect('/kids/footwear/', permanent=True)
    products = Product.objects.filter(
        category__category_name__iexact='kids',
        subcategory__subcategory_name__iexact='footwear',
        is_active=True,
    ).select_related('category', 'subcategory').prefetch_related('productvariant_set')
    page_background_image = _get_page_asset('kids_footwear', 'background', 'images/kids.webp')
    return render(request, 'store/product-listing-generic.html', {
        'page_title': 'Kids Footwear',
        'search_placeholder_text': 'Search kids footwear...',
        'image_cards': _build_product_cards(products),
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'page_background_image': page_background_image,
        'page_background_url': _asset_public_url(page_background_image),
        'detail_url_name': 'catalog_product_detail',
    })


def catalog_product_detail(request, product_id):
    product = get_object_or_404(
        Product.objects.select_related('category', 'subcategory', 'fashion_category', 'fashion_category__parent', 'fashion_category__root_category').prefetch_related('productvariant_set'),
        product_id=product_id,
        is_active=True,
    )
    product_url = _get_product_url(product)
    if product_url != request.path:
        return redirect(product_url, permanent=True)
    return _render_product_detail(request, product)



# Login page
def login_view(request):
    next_url = _safe_redirect_target(request, request.GET.get('next') or request.POST.get('next'), 'home')

    if request.method == 'POST':
        email = (request.POST.get('email') or '').strip()
        password = request.POST.get('password') or ''

        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            messages.error(request, 'This email is not registered. Please register first.')
        else:
            authenticated_user = authenticate(request, username=user.username, password=password)

            if authenticated_user is not None:
                login(request, authenticated_user)
                _merge_session_cart_into_db(request, authenticated_user)
                _set_session_cart(request, {})
                _set_session_profile_photo(request, authenticated_user)
                messages.success(request, 'Login Successful')
                return redirect(next_url)

            messages.error(request, 'Invalid password.')

    return render(request, 'store/login.html', {'next_url': next_url})


def logout_view(request):
    logout(request)
    return redirect('home')


def register_view(request):
    next_url = _safe_redirect_target(request, request.GET.get('next') or request.POST.get('next'), 'home')

    if request.method == 'POST':
        email = (request.POST.get('email') or '').strip().lower()
        password = request.POST.get('password') or ''
        confirm_password = request.POST.get('confirm_password') or ''

        if not email or not password:
            messages.error(request, 'Email and password are required.')
        elif password != confirm_password:
            messages.error(request, 'Passwords do not match.')
        elif User.objects.filter(email__iexact=email).exists():
            messages.error(request, 'An account with this email already exists.')
        elif User.objects.filter(username__iexact=email).exists():
            messages.error(request, 'An account with this email already exists.')
        else:
            user = User.objects.create_user(username=email, email=email, password=password)
            # Registration creates a local Django user.  Name the backend
            # explicitly because Google OAuth is configured alongside Django's
            # model backend.
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            _merge_session_cart_into_db(request, user)
            _set_session_cart(request, {})
            _set_session_profile_photo(request, user)
            return redirect(next_url)

    return render(request, 'store/register.html', {'next_url': next_url})

# Cart page
def cart(request):
    cart_data = _get_effective_cart_data(request)
    cart_items, subtotal, total_items = _build_cart_summary(cart_data)

    return render(request, 'store/cart.html', {
        'cart_items': cart_items,
        'subtotal': subtotal,
        'total_items': total_items,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'requires_login_for_checkout': not request.user.is_authenticated,
    })


def payment_gateway(request):
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next=/payment/")

    cart_data = _get_effective_cart_data(request)
    cart_items, subtotal, total_items = _build_cart_summary(cart_data)
    razorpay_configuration_error = _get_razorpay_configuration_error()
    default_address = UserAddress.objects.filter(user=request.user, is_default=True).first() or UserAddress.objects.filter(user=request.user).first()
    return render(request, 'store/payment.html', {
        'cart_items': cart_items,
        'subtotal': subtotal,
        'total_items': total_items,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'default_email': request.user.email,
        'default_full_name': request.user.get_full_name() or request.user.username,
        'default_phone_number': _get_default_phone_number(request),
        'default_address': default_address,
        'razorpay_mode': _get_razorpay_mode(),
        'razorpay_configuration_error': razorpay_configuration_error,
    })


def add_to_cart(request, product_id):
    if request.method != 'POST':
        return redirect('product_detail', product_id=product_id)

    product = get_object_or_404(Product, product_id=product_id, is_active=True)
    quantity = _parse_quantity(request.POST.get('quantity', 1))
    selected_size = (request.POST.get('selected_size') or '').strip() or None
    if request.user.is_authenticated:
        added, available_stock = _set_user_cart_item(request.user, product, quantity, selected_size=selected_size)
        _set_session_cart(request, {})
    else:
        added, available_stock = _add_to_session_cart(request, product, quantity, selected_size=selected_size)
    if not added:
        messages.error(request, 'This product is out of stock.')
        return redirect('product_detail', product_id=product_id)

    if quantity > available_stock:
        messages.warning(request, f'Only {available_stock} item(s) are available, so the quantity was limited.')

    return redirect('cart')


def remove_from_cart(request, cart_item_id):
    if request.method != 'POST':
        return redirect('cart')

    if request.user.is_authenticated:
        CartItem.objects.filter(user=request.user, cart_item_id=cart_item_id).delete()
        _set_session_cart(request, {})

    return redirect('cart')


def remove_session_cart_item(request, item_key):
    if request.method == 'POST':
        _remove_session_cart_item(request, item_key)
    return redirect('cart')


def update_cart_item(request, cart_item_id):
    if request.method != 'POST':
        return redirect('cart')

    quantity = _parse_quantity(request.POST.get('quantity') or 1)
    if request.user.is_authenticated:
        cart_item = CartItem.objects.filter(user=request.user, cart_item_id=cart_item_id).select_related('product').first()
        if cart_item:
            _set_user_cart_item(request.user, cart_item.product, quantity, selected_size=cart_item.selected_size, replace=True)
            _set_session_cart(request, {})
    return redirect('cart')


def update_session_cart_item(request, item_key):
    if request.method == 'POST':
        quantity = _parse_quantity(request.POST.get('quantity') or 1)
        session_cart = _get_session_cart(request)
        if item_key in session_cart:
            product = Product.objects.filter(product_id=session_cart[item_key]['product_id'], is_active=True).first()
            if product:
                _add_to_session_cart(
                    request,
                    product,
                    quantity,
                    selected_size=session_cart[item_key].get('selected_size'),
                    replace=True,
                )
    return redirect('cart')


def wishlist(request):
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next=/wishlist/")

    items = WishlistItem.objects.filter(user=request.user).select_related('product', 'product__category', 'product__subcategory')
    wishlist_cards = []
    for item in items:
        variant = ProductVariant.objects.filter(product=item.product).first()
        wishlist_cards.append({
            'wishlist_item': item,
            'product': item.product,
            'image_path': _get_product_image_path(item.product, variant),
        })

    return render(request, 'store/wishlist.html', {
        'wishlist_items': wishlist_cards,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
    })


def toggle_wishlist(request, product_id):
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next={reverse('catalog_product_detail', kwargs={'product_id': product_id})}")

    product = get_object_or_404(Product, product_id=product_id, is_active=True)
    wishlist_item = WishlistItem.objects.filter(user=request.user, product=product).first()
    if request.method == 'POST':
        if wishlist_item:
            wishlist_item.delete()
            messages.success(request, 'Product removed from wishlist.')
        else:
            WishlistItem.objects.create(user=request.user, product=product)
            messages.success(request, 'Product added to wishlist.')
    return redirect(request.POST.get('next') or 'wishlist')


def user_dashboard(request):
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next=/dashboard/")

    recent_orders = Order.objects.filter(user=request.user).prefetch_related('items__product')[:5]
    addresses = UserAddress.objects.filter(user=request.user)
    wishlist_items = WishlistItem.objects.filter(user=request.user).select_related('product')[:6]
    return render(request, 'store/user-dashboard.html', {
        'orders': recent_orders,
        'addresses': addresses,
        'wishlist_items': wishlist_items,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
    })


def profile_settings(request):
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next=/dashboard/profile/")

    if request.method == 'POST':
        request.user.first_name = (request.POST.get('first_name') or '').strip()
        request.user.last_name = (request.POST.get('last_name') or '').strip()
        email = (request.POST.get('email') or '').strip().lower()
        if email and not User.objects.filter(email__iexact=email).exclude(pk=request.user.pk).exists():
            request.user.email = email
            request.user.username = email
        request.user.save(update_fields=['first_name', 'last_name', 'email', 'username'])
        messages.success(request, 'Profile updated successfully.')
        return redirect('user_dashboard')

    return render(request, 'store/profile-settings.html', {
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
    })


def address_book(request):
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next=/dashboard/addresses/")

    if request.method == 'POST':
        address = UserAddress(
            user=request.user,
            full_name=(request.POST.get('full_name') or '').strip(),
            phone_number=(request.POST.get('phone_number') or '').strip(),
            address=(request.POST.get('address') or '').strip(),
            city=(request.POST.get('city') or '').strip(),
            state=(request.POST.get('state') or '').strip(),
            pincode=(request.POST.get('pincode') or '').strip(),
            country=(request.POST.get('country') or 'India').strip() or 'India',
            is_default=request.POST.get('is_default') == 'on',
        )
        if all([address.full_name, address.phone_number, address.address, address.city, address.state, address.pincode]):
            address.save()
            messages.success(request, 'Address saved successfully.')
        else:
            messages.error(request, 'Please fill all required address fields.')
        return redirect('address_book')

    return render(request, 'store/address-book.html', {
        'addresses': UserAddress.objects.filter(user=request.user),
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
    })


def buy_now(request, product_id):
    if request.method != 'POST':
        return redirect('product_detail', product_id=product_id)

    product = get_object_or_404(Product, product_id=product_id, is_active=True)
    quantity = _parse_quantity(request.POST.get('quantity', 1))
    selected_size = (request.POST.get('selected_size') or '').strip() or None
    variant = ProductVariant.objects.filter(product=product, size__iexact=selected_size).first() if selected_size else ProductVariant.objects.filter(product=product).first()
    available_stock = max(0, getattr(variant, 'quantity', 0) or 0) if selected_size else sum(ProductVariant.objects.filter(product=product).values_list('quantity', flat=True))
    if available_stock == 0:
        messages.error(request, 'This product is out of stock.')
        return redirect('product_detail', product_id=product_id)

    if quantity > available_stock:
        messages.warning(request, f'Only {available_stock} item(s) are available, so the quantity was limited.')

    if request.user.is_authenticated:
        CartItem.objects.filter(user=request.user).exclude(product=product).delete()
        _set_user_cart_item(request.user, product, quantity, selected_size=selected_size, replace=True)
        _set_session_cart(request, {})
    else:
        _set_session_cart(request, {})
        _add_to_session_cart(request, product, quantity, selected_size=selected_size, replace=True)
        messages.info(request, 'Please login to continue to payment. Your selected item is saved in the cart.')
        return redirect(f"{reverse('login')}?next=/payment/")

    return redirect('payment_gateway')


@transaction.atomic
def place_order(request):
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next=/payment/")

    if request.method != 'POST':
        return redirect('payment_gateway')

    cart_data = _get_effective_cart_data(request)
    cart_items, subtotal, total_items = _build_cart_summary(cart_data)

    if not cart_items:
        messages.error(request, 'Your cart is empty.')
        return redirect('cart')

    full_name = (request.POST.get('full_name') or '').strip()
    phone_number = (request.POST.get('phone_number') or '').strip()
    email = (request.POST.get('email') or '').strip()
    address = (request.POST.get('address') or '').strip()
    city = (request.POST.get('city') or '').strip()
    state = (request.POST.get('state') or '').strip()
    pincode = (request.POST.get('pincode') or '').strip()
    country = (request.POST.get('country') or 'India').strip() or 'India'
    payment_method = (request.POST.get('payment_method') or 'Card').strip() or 'Card'

    required_values = [full_name, phone_number, email, address, city, state, pincode]
    if any(not value for value in required_values):
        messages.error(request, 'Please fill in all billing details before placing the order.')
        return redirect('payment_gateway')

    order = Order.objects.create(
        user=request.user,
        full_name=full_name,
        phone_number=phone_number,
        email=email,
        address=address,
        city=city,
        state=state,
        pincode=pincode,
        country=country,
        payment_method=payment_method,
        subtotal=subtotal,
        total_items=total_items,
        status='Placed',
    )

    for item in cart_items:
        OrderItem.objects.create(
            order=order,
            product=item['product'],
            selected_size=item.get('selected_size'),
            quantity=item['quantity'],
            unit_price=item['product'].offer_price,
            line_total=item['line_total'],
        )

    CartItem.objects.filter(user=request.user).delete()
    _set_session_cart(request, {})
    messages.success(request, f'Order #{order.order_id} placed successfully.')
    return redirect('order_success', order_id=order.order_id)


@transaction.atomic
def start_payment_gateway(request):
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next=/payment/")

    if request.method != 'POST':
        return redirect('payment_gateway')

    razorpay_error = _get_razorpay_configuration_error()
    if razorpay_error:
        messages.error(request, razorpay_error)
        return redirect('payment_gateway')

    cart_data = _get_effective_cart_data(request)
    cart_items, subtotal, total_items = _build_cart_summary(cart_data)

    if not cart_items:
        messages.error(request, 'Your cart is empty.')
        return redirect('cart')

    full_name = (request.POST.get('full_name') or '').strip()
    phone_number = (request.POST.get('phone_number') or '').strip()
    email = (request.POST.get('email') or '').strip()
    address = (request.POST.get('address') or '').strip()
    city = (request.POST.get('city') or '').strip()
    state = (request.POST.get('state') or '').strip()
    pincode = (request.POST.get('pincode') or '').strip()
    country = (request.POST.get('country') or 'India').strip() or 'India'

    required_values = [full_name, phone_number, email, address, city, state, pincode]
    if any(not value for value in required_values):
        messages.error(request, 'Please fill in all billing details before continuing to payment.')
        return redirect('payment_gateway')

    amount_paise = _amount_to_paise(subtotal)

    order = Order.objects.create(
        user=request.user,
        full_name=full_name,
        phone_number=phone_number,
        email=email,
        address=address,
        city=city,
        state=state,
        pincode=pincode,
        country=country,
        payment_method='Razorpay Payment Link',
        payment_status='Created',
        currency='INR',
        subtotal=subtotal,
        total_items=total_items,
        status='Pending Payment',
    )

    for item in cart_items:
        OrderItem.objects.create(
            order=order,
            product=item['product'],
            selected_size=item.get('selected_size'),
            quantity=item['quantity'],
            unit_price=item['product'].offer_price,
            line_total=item['line_total'],
        )

    callback_url = _build_payment_callback_url(request, order.order_id)
    reference_id = f"shopease_{order.order_id}"[:40]

    try:
        payment_link = _create_razorpay_payment_link(
            amount_paise=amount_paise,
            callback_url=callback_url,
            reference_id=reference_id,
            description=f"ShopEase Order #{order.order_id}",
            customer={
                'name': full_name,
                'email': email,
                'contact': phone_number,
            },
            notes={
                'local_order_id': str(order.order_id),
                'user_id': str(request.user.id),
            },
        )
    except (requests.RequestException, ValueError) as exc:
        order.payment_status = 'Failed'
        order.status = 'Payment Link Failed'
        order.save(update_fields=['payment_status', 'status'])
        messages.error(request, _friendly_razorpay_error(exc))
        return redirect('payment_gateway')

    order.razorpay_order_id = payment_link.get('id')
    order.save(update_fields=['razorpay_order_id'])

    payment_link_url = payment_link.get('short_url') or payment_link.get('payment_link') or payment_link.get('short_url')
    if not payment_link_url:
        messages.error(request, 'Unable to open the payment gateway right now. Please try again.')
        return redirect('payment_gateway')

    return redirect(payment_link_url)


@transaction.atomic
def razorpay_payment_link_callback(request):
    local_order_id = (request.GET.get('local_order_id') or '').strip()
    payment_link_id = (request.GET.get('razorpay_payment_link_id') or '').strip()
    payment_link_reference_id = (request.GET.get('razorpay_payment_link_reference_id') or '').strip()
    payment_link_status = (request.GET.get('razorpay_payment_link_status') or '').strip().lower()
    payment_id = (request.GET.get('razorpay_payment_id') or '').strip()
    razorpay_signature = (request.GET.get('razorpay_signature') or '').strip()

    if not local_order_id:
        local_order_id = _extract_order_id_from_reference_id(payment_link_reference_id)

    if not local_order_id:
        messages.error(request, 'Order reference is missing from the payment gateway response.')
        return redirect('payment_gateway')

    order = get_object_or_404(Order.objects.select_for_update().prefetch_related('items__product'), order_id=local_order_id)
    expected_reference_id = f"shopease_{order.order_id}"

    if payment_link_reference_id and payment_link_reference_id != expected_reference_id:
        messages.error(request, 'Payment reference did not match the order.')
        return redirect('payment_gateway')

    if not payment_link_id:
        messages.error(request, 'Payment gateway did not return a payment link id.')
        return redirect('payment_gateway')

    if order.razorpay_order_id and order.razorpay_order_id != payment_link_id:
        messages.error(request, 'Payment gateway response does not match the pending order.')
        return redirect('payment_gateway')

    if not payment_id or not payment_link_status or not razorpay_signature:
        messages.error(request, 'Payment gateway did not return complete confirmation details.')
        return redirect('payment_gateway')

    if not _verify_razorpay_payment_link_signature(
        payment_link_id=payment_link_id,
        reference_id=expected_reference_id,
        payment_link_status=payment_link_status,
        payment_id=payment_id,
        signature=razorpay_signature,
    ):
        messages.error(request, 'Payment signature verification failed.')
        return redirect('payment_gateway')

    try:
        payment_link_data = _fetch_razorpay_payment_link(payment_link_id)
    except (requests.RequestException, ValueError) as exc:
        messages.error(request, _friendly_razorpay_error(exc))
        return redirect('payment_gateway')

    expected_amount = _amount_to_paise(order.subtotal)
    actual_status = (payment_link_data.get('status') or payment_link_status or '').lower()
    amount_paid = int(payment_link_data.get('amount_paid') or 0)
    reference_id = payment_link_data.get('reference_id') or ''

    if reference_id and reference_id != expected_reference_id:
        messages.error(request, 'Payment reference did not match the order.')
        return redirect('payment_gateway')

    if actual_status != 'paid' or amount_paid < expected_amount:
        _update_order_payment_record(
            order,
            payment_method='Razorpay Payment Link',
            payment_status=actual_status.title() if actual_status else 'Pending',
            currency=payment_link_data.get('currency') or 'INR',
            razorpay_order_id=payment_link_id,
            status='Pending Payment',
        )
        messages.error(request, 'Payment is not completed yet. Please complete the payment and try again.')
        return redirect('payment_gateway')

    payment_records = payment_link_data.get('payments') or []
    if payment_records and not payment_id:
        payment_id = payment_records[0].get('payment_id') or payment_records[0].get('id') or ''

    if request.user.is_authenticated and order.user_id != request.user.id:
        messages.error(request, 'This payment callback belongs to a different user account.')
        return redirect('home')

    _update_order_payment_record(
        order,
        currency=payment_link_data.get('currency') or 'INR',
        razorpay_order_id=payment_link_id,
        razorpay_payment_id=payment_id,
        razorpay_signature=razorpay_signature,
    )

    _finalize_paid_order(
        order,
        request=request,
        payment_method='Razorpay Payment Link',
        payment_status='Paid',
        payment_id=payment_id,
    )

    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next={reverse('order_success', kwargs={'order_id': order.order_id})}")

    messages.success(request, f'Payment completed and order #{order.order_id} confirmed successfully.')
    return redirect('order_success', order_id=order.order_id)


@transaction.atomic
def verify_razorpay_payment(request):
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next=/payment/")

    if request.method != 'POST':
        return redirect('payment_gateway')

    local_order_id = (request.POST.get('local_order_id') or '').strip()
    razorpay_order_id = (request.POST.get('razorpay_order_id') or '').strip()
    razorpay_payment_id = (request.POST.get('razorpay_payment_id') or '').strip()
    razorpay_signature = (request.POST.get('razorpay_signature') or '').strip()

    required_values = [local_order_id, razorpay_order_id, razorpay_payment_id, razorpay_signature]
    if any(not value for value in required_values):
        messages.error(request, 'Payment verification details are missing. Please try again.')
        return redirect('payment_gateway')

    razorpay_error = _get_razorpay_configuration_error()
    if razorpay_error:
        messages.error(request, razorpay_error)
        return redirect('payment_gateway')

    if not _verify_razorpay_signature(razorpay_order_id, razorpay_payment_id, razorpay_signature):
        messages.error(request, 'Payment signature verification failed.')
        return redirect('payment_gateway')

    order = get_object_or_404(
        Order.objects.select_for_update().prefetch_related('items__product'),
        order_id=local_order_id,
        user=request.user,
        razorpay_order_id=razorpay_order_id,
    )

    try:
        payment_data = _fetch_razorpay_payment(razorpay_payment_id)
    except requests.RequestException:
        messages.error(request, 'Unable to confirm the payment with Razorpay right now. Please try again.')
        return redirect('payment_gateway')

    expected_amount = _amount_to_paise(order.subtotal)
    payment_status = (payment_data.get('status') or '').lower()
    payment_amount = int(payment_data.get('amount') or 0)
    payment_order_id = payment_data.get('order_id') or ''

    if payment_order_id != razorpay_order_id or payment_amount != expected_amount or payment_status not in {'authorized', 'captured'}:
        messages.error(request, 'Payment could not be validated against the order details.')
        return redirect('payment_gateway')

    _update_order_payment_record(
        order,
        currency=payment_data.get('currency') or 'INR',
        razorpay_order_id=razorpay_order_id,
        razorpay_payment_id=razorpay_payment_id,
        razorpay_signature=razorpay_signature,
    )

    _finalize_paid_order(
        order,
        request=request,
        payment_method='Razorpay',
        payment_status=payment_status.title(),
        payment_id=razorpay_payment_id,
    )
    messages.success(request, f'Payment verified and order #{order.order_id} placed successfully.')
    return redirect('order_success', order_id=order.order_id)


@transaction.atomic
def create_razorpay_checkout(request):
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'message': 'Login required.'}, status=401)

    if request.method != 'POST':
        return JsonResponse({'ok': False, 'message': 'Invalid request method.'}, status=405)

    razorpay_error = _get_razorpay_configuration_error()
    if razorpay_error:
        return JsonResponse({'ok': False, 'message': razorpay_error}, status=400)

    cart_data = _get_effective_cart_data(request)
    cart_items, subtotal, total_items = _build_cart_summary(cart_data)

    if not cart_items:
        return JsonResponse({'ok': False, 'message': 'Your cart is empty.'}, status=400)

    full_name = (request.POST.get('full_name') or '').strip()
    phone_number = (request.POST.get('phone_number') or '').strip()
    email = (request.POST.get('email') or '').strip()
    address = (request.POST.get('address') or '').strip()
    city = (request.POST.get('city') or '').strip()
    state = (request.POST.get('state') or '').strip()
    pincode = (request.POST.get('pincode') or '').strip()
    country = (request.POST.get('country') or 'India').strip() or 'India'

    required_values = [full_name, phone_number, email, address, city, state, pincode]
    if any(not value for value in required_values):
        return JsonResponse({'ok': False, 'message': 'Please fill in all billing details.'}, status=400)

    amount_paise = _amount_to_paise(subtotal)

    order = Order.objects.create(
        user=request.user,
        full_name=full_name,
        phone_number=phone_number,
        email=email,
        address=address,
        city=city,
        state=state,
        pincode=pincode,
        country=country,
        payment_method='Razorpay',
        payment_status='Created',
        currency='INR',
        subtotal=subtotal,
        total_items=total_items,
        status='Pending Payment',
    )

    for item in cart_items:
        OrderItem.objects.create(
            order=order,
            product=item['product'],
            selected_size=item.get('selected_size'),
            quantity=item['quantity'],
            unit_price=item['product'].offer_price,
            line_total=item['line_total'],
        )

    try:
        razorpay_order = _create_razorpay_order(
            amount_paise=amount_paise,
            receipt=_build_razorpay_receipt(request.user.id),
            notes={
                'user_id': str(request.user.id),
                'local_order_id': str(order.order_id),
                'email': email,
            },
        )
    except (requests.RequestException, ValueError) as exc:
        order.payment_status = 'Failed'
        order.status = 'Payment Failed'
        order.save(update_fields=['payment_status', 'status'])
        return JsonResponse({'ok': False, 'message': _friendly_razorpay_error(exc)}, status=400)

    order.razorpay_order_id = razorpay_order.get('id')
    order.save(update_fields=['razorpay_order_id'])

    return JsonResponse({
        'ok': True,
        'local_order_id': order.order_id,
        'razorpay_order_id': razorpay_order.get('id', ''),
        'amount': amount_paise,
        'currency': razorpay_order.get('currency', 'INR'),
        'key_id': getattr(settings, 'RAZORPAY_KEY_ID', ''),
        'customer': {
            'name': full_name,
            'email': email,
            'contact': phone_number,
        },
    })


@csrf_exempt
@transaction.atomic
def razorpay_webhook(request):
    if request.method != 'POST':
        return HttpResponse(status=405)

    webhook_signature = (request.headers.get('X-Razorpay-Signature') or '').strip()
    raw_body = request.body or b''

    if not _verify_razorpay_webhook_signature(raw_body, webhook_signature):
        return JsonResponse({'ok': False, 'message': 'Invalid webhook signature.'}, status=400)

    try:
        webhook_event = json.loads(raw_body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({'ok': False, 'message': 'Invalid webhook payload.'}, status=400)

    event_name = (webhook_event.get('event') or '').strip().lower()
    order, payment_entity, order_entity, payment_link_entity = _get_order_from_webhook_payload(webhook_event.get('payload'))

    if order is None:
        return JsonResponse({'ok': True, 'ignored': True})

    payment_status = (
        str(payment_entity.get('status') or '').strip().lower()
        or str(order_entity.get('status') or '').strip().lower()
        or str(payment_link_entity.get('status') or '').strip().lower()
    )
    payment_id = str(payment_entity.get('id') or '').strip()
    currency = (
        str(payment_entity.get('currency') or '').strip()
        or str(order_entity.get('currency') or '').strip()
        or str(payment_link_entity.get('currency') or '').strip()
        or 'INR'
    )
    expected_amount = _amount_to_paise(order.subtotal)
    amount_paid = int(
        payment_entity.get('amount_captured')
        or payment_entity.get('amount')
        or order_entity.get('amount_paid')
        or payment_link_entity.get('amount_paid')
        or 0
    )
    razorpay_order_id = str(order_entity.get('id') or payment_entity.get('order_id') or '').strip()
    payment_link_id = str(payment_link_entity.get('id') or '').strip()

    if event_name in {'payment.authorized', 'payment.failed'}:
        status_label = payment_status.title() if payment_status else ('Failed' if event_name == 'payment.failed' else 'Authorized')
        order_status = 'Payment Failed' if event_name == 'payment.failed' else 'Pending Payment'
        _update_order_payment_record(
            order,
            payment_method='Razorpay Payment Link' if event_name.startswith('payment_link.') else 'Razorpay',
            payment_status=status_label,
            currency=currency,
            razorpay_order_id=payment_link_id or razorpay_order_id,
            razorpay_payment_id=payment_id,
            razorpay_signature=webhook_signature,
            status=order_status,
        )
        return JsonResponse({'ok': True, 'event': event_name})

    if event_name not in {'order.paid', 'payment.captured', 'payment_link.paid'}:
        return JsonResponse({'ok': True, 'ignored': True, 'event': event_name})

    if amount_paid < expected_amount:
        _update_order_payment_record(
            order,
            payment_method='Razorpay Payment Link' if event_name == 'payment_link.paid' else 'Razorpay',
            payment_status=payment_status.title() if payment_status else 'Pending',
            currency=currency,
            razorpay_order_id=payment_link_id or razorpay_order_id,
            razorpay_payment_id=payment_id,
            razorpay_signature=webhook_signature,
            status='Pending Payment',
        )
        return JsonResponse({'ok': True, 'pending': True, 'event': event_name})

    _update_order_payment_record(
        order,
        currency=currency,
        razorpay_order_id=payment_link_id or razorpay_order_id,
        razorpay_payment_id=payment_id,
        razorpay_signature=webhook_signature,
    )

    _finalize_paid_order(
        order,
        payment_method='Razorpay Payment Link' if event_name == 'payment_link.paid' else 'Razorpay',
        payment_status='Paid' if event_name in {'order.paid', 'payment_link.paid'} else payment_status.title(),
        payment_id=payment_id,
    )
    return JsonResponse({'ok': True, 'event': event_name, 'order_id': order.order_id})


def order_success(request, order_id):
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next=/orders/{order_id}/success/")

    order = get_object_or_404(Order.objects.prefetch_related('items__product'), order_id=order_id, user=request.user)
    order_items = []
    for item in order.items.all():
        variant = ProductVariant.objects.filter(
            product=item.product,
            size__iexact=item.selected_size,
        ).first() if item.selected_size else ProductVariant.objects.filter(product=item.product).first()
        order_items.append({
            'product': item.product,
            'selected_size': item.selected_size,
            'quantity': item.quantity,
            'unit_price': item.unit_price,
            'line_total': item.line_total,
            'image_path': _get_product_image_path(item.product, variant),
        })

    invoice_delivery_notice = request.session.get('invoice_delivery_notice') or {}
    if invoice_delivery_notice.get('order_id') != order.order_id:
        invoice_delivery_notice = {
            'email': order.email,
            'phone_number': order.phone_number,
            'email_sent': False,
        }

    return render(request, 'store/order-success.html', {
        'order': order,
        'order_items': order_items,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'invoice_delivery_notice': invoice_delivery_notice,
    })


def order_tracking(request, order_id):
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next=/orders/{order_id}/tracking/")

    order = get_object_or_404(Order.objects.prefetch_related('items__product'), order_id=order_id, user=request.user)
    return render(request, 'store/order-tracking.html', {
        'order': order,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
    })


def order_tracking_api(request, order_id):
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False, 'message': 'Authentication required.'}, status=401)
    order = get_object_or_404(Order, order_id=order_id, user=request.user)
    return JsonResponse({
        'ok': True,
        'order_id': order.order_id,
        'status': order.status,
        'payment_status': order.payment_status,
        'payment_method': order.payment_method,
        'created_at': order.created_at.strftime('%B %d, %Y, %I:%M %p'),
        'estimated_delivery': order.estimated_delivery.strftime('%B %d, %Y') if order.estimated_delivery else '',
        'courier_name': order.courier_name or '',
        'tracking_number': order.tracking_number or '',
        'tracking_url': order.tracking_url or '',
    })


def orders(request):
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next=/orders/")

    user_orders = Order.objects.filter(user=request.user).prefetch_related('items__product')
    return render(request, 'store/orders.html', {
        'orders': user_orders,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
    })


def _is_allowed_admin_user(user):
    """Restrict the ShopEase control centre to Django administrative users.

    Authorizing a customer because their supplied email matches a setting is
    unsafe: registration accepts arbitrary email addresses and an email is not
    an authorization boundary.  Keep the custom control centre aligned with
    Django's built-in admin instead.
    """
    if not user or not user.is_authenticated or not user.is_active:
        return False
    return bool(user.is_staff or user.is_superuser)


def _find_admin_user(identifier):
    normalized_identifier = (identifier or '').strip()
    if not normalized_identifier:
        return None

    user = User.objects.filter(
        Q(username__iexact=normalized_identifier) | Q(email__iexact=normalized_identifier)
    ).first()
    if user and _is_allowed_admin_user(user):
        return user
    return None


def _is_shop_admin(user):
    return _is_allowed_admin_user(user)


def _require_shop_admin(request):
    if _is_shop_admin(request.user):
        return None
    if _is_ajax_request(request):
        return JsonResponse({
            'success': False,
            'message': 'Admin login is required. Please sign in again.',
        }, status=401)
    return redirect(f"{reverse('admin_access')}?next={request.path}")


def admin_access(request):
    next_url = _safe_redirect_target(request, request.GET.get('next') or request.POST.get('next'), reverse('admin_control_center'))
    submitted_identifier = (request.POST.get('identifier') or request.POST.get('email') or '').strip()

    if _is_shop_admin(request.user):
        return redirect(next_url)

    if request.method == 'POST':
        admin_identifier = submitted_identifier
        password = request.POST.get('password') or ''
        user = _find_admin_user(admin_identifier)

        if user is None:
            messages.error(request, 'Use a staff or superuser account to access this panel.')
        else:
            authenticated_user = authenticate(request, username=user.username, password=password)
            if authenticated_user is None:
                messages.error(request, 'Invalid admin password.')
            elif not _is_shop_admin(authenticated_user):
                messages.error(request, 'This account does not have admin panel access.')
            else:
                login(request, authenticated_user)
                return redirect(next_url)

    return render(request, 'store/admin-login.html', {
        'next_url': next_url,
        'admin_identifier': submitted_identifier,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
    })


def admin_dashboard(request):
    return redirect('admin_control_center')


def _build_admin_background_rows():
    site_keys = {choice['asset_key'] for choice in BACKGROUND_ASSET_CHOICES if choice['scope'] == 'site'}
    page_keys = {(choice['page_key'], choice['asset_key']) for choice in BACKGROUND_ASSET_CHOICES if choice['scope'] == 'page'}
    try:
        site_assets = {
            asset.asset_key: asset
            for asset in SiteAsset.objects.filter(asset_key__in=site_keys)
        }
        page_assets = {
            (asset.page_key, asset.asset_key): asset
            for asset in PageAsset.objects.filter(
                page_key__in={page_key for page_key, _ in page_keys},
                asset_key__in={asset_key for _, asset_key in page_keys},
            )
        }
    except (ProgrammingError, OperationalError):
        site_assets, page_assets = {}, {}

    background_rows = []
    for choice in BACKGROUND_ASSET_CHOICES:
        lookup_key = choice['asset_key'] if choice['scope'] == 'site' else (choice['page_key'], choice['asset_key'])
        asset = (site_assets if choice['scope'] == 'site' else page_assets).get(lookup_key)
        saved_path = getattr(asset, 'image_path', '') or ''
        if getattr(asset, 'is_active', True) and saved_path:
            normalized = saved_path.replace('\\', '/').lstrip('/')
            current_path = _asset_exists(normalized) or _asset_exists(choice['fallback']) or choice['fallback']
        else:
            current_path = _asset_exists(choice['fallback']) or choice['fallback']

        background_rows.append({
            **choice,
            'choice_id': f"{choice['scope']}:{choice['page_key']}:{choice['asset_key']}",
            'saved_path': saved_path,
            'current_path': current_path,
            'current_url': _asset_public_url(current_path),
            'is_active': getattr(asset, 'is_active', True),
        })
    return background_rows


def _build_admin_product_rows(products, outgoing_by_product=None):
    product_rows = []
    outgoing_by_product = outgoing_by_product or {}
    for product in products:
        variant = _get_first_variant(product)
        hierarchy_label = product.fashion_category.full_path if product.fashion_category_id else f'{product.category.category_name} > {product.subcategory.subcategory_name}'
        product_rows.append({
            'product': product,
            'variant': variant,
            'total_stock': _get_total_stock(product),
            'outgoing_units': outgoing_by_product.get(product.product_id, 0) or 0,
            'image_path': _get_product_image_path(product, variant),
            'hierarchy_label': hierarchy_label,
        })
    return product_rows



def _admin_product_payload(product):
    product = Product.objects.select_related('category', 'subcategory', 'fashion_category', 'fashion_category__parent').prefetch_related('productvariant_set').get(product_id=product.product_id)
    variant = _get_first_variant(product)
    image_path = _get_product_image_path(product, variant)
    hierarchy_label = product.fashion_category.full_path if product.fashion_category_id else f'{product.category.category_name} > {product.subcategory.subcategory_name}'
    return {
        'id': product.product_id,
        'name': product.product_name,
        'brand': product.brand or 'No brand',
        'sku': product.sku,
        'price': str(product.offer_price),
        'stock': _get_total_stock(product),
        'active': product.is_active,
        'featured': product.is_featured,
        'image_path': image_path,
        'image_url': _asset_public_url(image_path),
        'hierarchy_label': hierarchy_label,
    }



def _category_filter_group_id(category):
    if not category.parent_id:
        return ''
    if category.parent and category.parent.parent_id:
        return str(category.parent_id)
    return str(category.fashion_category_id)


def _build_admin_category_filter_options(root_nodes):
    root_ids = [root.fashion_category_id for root in root_nodes]
    if not root_ids:
        return []
    return list(
        FashionCategory.objects.filter(parent_id__in=root_ids)
        .select_related('root_category', 'parent')
        .order_by('root_category__category_name', 'sort_order', 'fashion_category_id')
    )


def _build_admin_customer_rows(customers):
    rows = []
    for customer in customers:
        orders_qs = Order.objects.filter(user=customer)
        rows.append({
            'customer': customer,
            'order_count': orders_qs.count(),
            'total_spent': orders_qs.filter(payment_status__in=['Paid', 'Authorized', 'Captured']).aggregate(total=Sum('subtotal'))['total'] or Decimal('0'),
            'latest_order': orders_qs.first(),
        })
    return rows


def admin_control_center(request):
    access_redirect = _require_shop_admin(request)
    if access_redirect:
        return access_redirect

    if request.method == 'POST':
        action = (request.POST.get('action') or '').strip()
        if action == 'save_home_content':
            home_content = HomeContent.objects.filter(is_active=True).first() or HomeContent(is_active=True)
            content_fields = [
                'hero_subtitle', 'hero_title', 'hero_highlight', 'hero_tagline',
                'hero_button_text', 'hero_button_url', 'search_placeholder',
                'search_button_text', 'category_section_title',
                'footer_text', 'footer_brand_text', 'footer_builder_text',
            ]
            for field in content_fields:
                setattr(home_content, field, (request.POST.get(field) or '').strip() or None)
            home_content.save()
            response = _json_admin_response(request, True, 'Website settings updated successfully.')
            if response:
                return response
            messages.success(request, 'Website settings updated successfully.')
            return redirect('admin_control_center')

        if action == 'save_site_asset':
            asset_key = (request.POST.get('asset_key') or '').strip()
            typed_path = _normalize_admin_asset_path(request.POST.get('image_path'))
            try:
                uploaded_path = _save_uploaded_asset_image(request.FILES.get('asset_upload'), asset_key or 'site-asset')
            except ValueError as exc:
                response = _json_admin_response(request, False, str(exc))
                if response:
                    return response
                messages.error(request, str(exc))
                return redirect('admin_control_center')
            final_path = uploaded_path or typed_path
            if not asset_key or not final_path:
                response = _json_admin_response(request, False, 'Asset key and image are required.')
                if response:
                    return response
                messages.error(request, 'Asset key and image are required.')
            elif not _asset_exists(final_path):
                response = _json_admin_response(request, False, 'That asset image was not found.')
                if response:
                    return response
                messages.error(request, 'That asset image was not found.')
            else:
                asset = SiteAsset.objects.filter(asset_key=asset_key).first() or SiteAsset(asset_key=asset_key)
                previous_path = asset.image_path
                asset.image_path = final_path
                asset.is_active = request.POST.get('is_active') == 'on'
                asset.save()
                _delete_replaced_uploaded_assets([previous_path], [final_path])
                response = _json_admin_response(request, True, f'{asset_key} asset updated successfully.', {
                    'asset': {
                        'key': asset.asset_key,
                        'path': asset.image_path,
                        'url': _asset_public_url(asset.image_path),
                    }
                })
                if response:
                    return response
                messages.success(request, f'{asset_key} asset updated successfully.')
            return redirect('admin_control_center')

    low_stock_threshold = 5
    product_filters = {
        'search': (request.GET.get('product_search') or '').strip(),
        'category': (request.GET.get('product_category') or '').strip(),
        'subcategory': (request.GET.get('product_subcategory') or '').strip(),
        'collection': (request.GET.get('product_collection') or '').strip(),
        'status': (request.GET.get('product_status') or '').strip(),
        'stock': (request.GET.get('stock_status') or '').strip(),
        'min_price': (request.GET.get('min_price') or '').strip(),
        'max_price': (request.GET.get('max_price') or '').strip(),
        'movement': (request.GET.get('product_movement') or '').strip(),
    }

    products = Product.objects.select_related(
        'category', 'subcategory', 'fashion_category',
        'fashion_category__parent', 'fashion_category__root_category',
    ).prefetch_related('productvariant_set').annotate(
        total_stock_value=Sum('productvariant__quantity'),
    ).order_by('-product_id')
    if product_filters['search']:
        search_term = product_filters['search']
        products = products.filter(
            Q(product_name__icontains=search_term)
            | Q(sku__icontains=search_term)
            | Q(brand__icontains=search_term)
        )
    if product_filters['category'].isdigit():
        products = products.filter(category_id=int(product_filters['category']))
    if product_filters['subcategory'].isdigit():
        products = products.filter(subcategory_id=int(product_filters['subcategory']))
    if product_filters['collection'].isdigit():
        products = products.filter(fashion_category__root_category_id=int(product_filters['collection']))
    if product_filters['status'] == 'active':
        products = products.filter(is_active=True)
    elif product_filters['status'] == 'hidden':
        products = products.filter(is_active=False)
    if product_filters['movement'] == 'has_outgoing':
        products = products.filter(orderitem__order__payment_status__in=['Paid', 'Authorized', 'Captured']).distinct()
    elif product_filters['movement'] == 'no_outgoing':
        products = products.exclude(orderitem__order__payment_status__in=['Paid', 'Authorized', 'Captured']).distinct()
    if product_filters['stock'] == 'in_stock':
        products = products.filter(total_stock_value__gt=low_stock_threshold)
    elif product_filters['stock'] == 'low_stock':
        products = products.filter(total_stock_value__gt=0, total_stock_value__lte=low_stock_threshold)
    elif product_filters['stock'] == 'out_of_stock':
        products = products.filter(Q(total_stock_value__lte=0) | Q(total_stock_value__isnull=True))
    min_price = _parse_decimal(product_filters['min_price'], default='0') if product_filters['min_price'] else None
    max_price = _parse_decimal(product_filters['max_price'], default='0') if product_filters['max_price'] else None
    if min_price is not None:
        products = products.filter(offer_price__gte=min_price)
    if max_price is not None:
        products = products.filter(offer_price__lte=max_price)
    filtered_product_count = products.count()
    product_page = Paginator(products, 25).get_page(request.GET.get('page') or 1)
    outgoing_by_product = dict(
        OrderItem.objects.filter(order__payment_status__in=['Paid', 'Authorized', 'Captured'])
        .values('product_id')
        .annotate(total=Sum('quantity'))
        .values_list('product_id', 'total')
    )
    product_rows = _build_admin_product_rows(product_page.object_list, outgoing_by_product)
    page_query = request.GET.copy()
    page_query.pop('page', None)
    page_query_params = page_query.urlencode()

    all_products = Product.objects.annotate(total_stock_value=Sum('productvariant__quantity'))
    total_products = all_products.count()
    active_products = all_products.filter(is_active=True).count()
    out_of_stock_count = all_products.filter(Q(total_stock_value__lte=0) | Q(total_stock_value__isnull=True)).count()
    low_stock_count = all_products.filter(total_stock_value__gt=0, total_stock_value__lte=low_stock_threshold).count()
    available_products = all_products.filter(total_stock_value__gt=0).count()
    in_stock_count = all_products.filter(total_stock_value__gt=low_stock_threshold).count()
    outgoing_units = OrderItem.objects.filter(
        order__payment_status__in=['Paid', 'Authorized', 'Captured'],
    ).aggregate(total=Sum('quantity'))['total'] or 0
    revenue = Order.objects.filter(payment_status__in=['Paid', 'Authorized', 'Captured']).aggregate(total=Sum('subtotal'))['total'] or Decimal('0')
    total_orders = Order.objects.count()
    total_customers = User.objects.filter(is_staff=False, is_superuser=False).count()
    categories = Category.objects.order_by('category_name')
    subcategories = SubCategory.objects.select_related('category').order_by('subcategory_name')
    fashion_categories = FashionCategory.objects.filter(is_active=True, level__gte=2).select_related('root_category', 'parent').order_by('root_category__category_name', 'level', 'sort_order', 'fashion_category_id')
    all_fashion_categories = FashionCategory.objects.select_related('root_category', 'parent').order_by('root_category__category_name', 'level', 'sort_order', 'fashion_category_id')
    orders = Order.objects.select_related('user').prefetch_related('items__product').order_by('-created_at')
    customers = User.objects.filter(is_staff=False, is_superuser=False).order_by('-date_joined')
    variants = ProductVariant.objects.select_related('product', 'product__category', 'product__subcategory').order_by('quantity', 'product__product_name')
    root_nodes = list(FashionCategory.objects.filter(parent__isnull=True).select_related('root_category').order_by('sort_order', 'fashion_category_id'))
    card_groups = []
    for root_node in root_nodes:
        cards = list(
            FashionCategory.objects.filter(root_category=root_node.root_category)
            .exclude(fashion_category_id=root_node.fashion_category_id)
            .select_related('root_category', 'parent', 'parent__parent')
            .order_by('level', 'sort_order', 'fashion_category_id')
        )
        for category in cards:
            category.filter_group_id = _category_filter_group_id(category)
        card_groups.append({
            'root': root_node,
            'cards': cards,
        })
    category_filter_options = _build_admin_category_filter_options(root_nodes)
    home_content = HomeContent.objects.filter(is_active=True).first()

    return render(request, 'store/admin-control-center.html', {
        'products': product_rows,
        'product_page': product_page,
        'page_query_params': page_query_params,
        'filtered_product_count': filtered_product_count,
        'product_filters': product_filters,
        'product_collection_options': root_nodes,
        'orders': orders[:25],
        'customers': _build_admin_customer_rows(customers[:25]),
        'variants': variants[:100],
        'categories': categories,
        'subcategories': subcategories,
        'fashion_categories': fashion_categories,
        'all_fashion_categories': all_fashion_categories,
        'card_groups': card_groups,
        'category_filter_options': category_filter_options,
        'backgrounds': _build_admin_background_rows(),
        'home_content': home_content,
        'site_assets': SiteAsset.objects.order_by('asset_key'),
        'total_products': total_products,
        'active_products': active_products,
        'total_categories': categories.count(),
        'total_subcategories': subcategories.count(),
        'available_products': available_products,
        'outgoing_units': outgoing_units,
        'out_of_stock_count': out_of_stock_count,
        'stock_flow_total': total_products,
        'stock_flow_in_end': round((in_stock_count / total_products) * 100) if total_products else 0,
        'stock_flow_low_end': round(((in_stock_count + low_stock_count) / total_products) * 100) if total_products else 0,
        'stock_flow': [
            {'label': 'In stock', 'value': in_stock_count, 'class': 'flow-available', 'percentage': round((in_stock_count / total_products) * 100) if total_products else 0},
            {'label': 'Low stock', 'value': low_stock_count, 'class': 'flow-low', 'percentage': round((low_stock_count / total_products) * 100) if total_products else 0},
            {'label': 'Out of stock', 'value': out_of_stock_count, 'class': 'flow-out', 'percentage': round((out_of_stock_count / total_products) * 100) if total_products else 0},
        ],
        'total_orders': total_orders,
        'total_customers': total_customers,
        'revenue': revenue,
        'low_stock_count': low_stock_count,
        'low_stock_threshold': low_stock_threshold,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
    })


def _parse_decimal(raw_value, default='0'):
    try:
        return Decimal((raw_value or default).strip())
    except (InvalidOperation, AttributeError):
        return Decimal(default)


def _parse_size_quantities(raw_value):
    variants = []
    if not raw_value:
        return variants

    lines = [line.strip() for line in raw_value.replace(',', '\n').splitlines() if line.strip()]
    for line in lines:
        if ':' not in line:
            continue
        size, quantity = line.split(':', 1)
        size = size.strip()
        quantity = quantity.strip()
        if not size:
            continue
        try:
            parsed_quantity = max(0, int(quantity))
        except ValueError:
            continue
        variants.append((size, parsed_quantity))
    return variants


def _parse_int(raw_value, default=0):
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return default


def _get_total_stock(product):
    return sum((variant.quantity or 0) for variant in _get_product_variants(product))


ALLOWED_UPLOAD_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
DB_UPLOAD_PREFIX = 'dbuploads'
UPLOAD_PATH_PREFIXES = {('uploads',), (DB_UPLOAD_PREFIX,)}
MAX_UPLOAD_IMAGE_BYTES = int(getattr(settings, 'MAX_UPLOAD_IMAGE_BYTES', 10 * 1024 * 1024))


def _is_ajax_request(request):
    return request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.headers.get('accept') == 'application/json'


def _json_admin_response(request, success, message, payload=None, status=None):
    if not _is_ajax_request(request):
        return None
    return JsonResponse({
        'success': bool(success),
        'message': message,
        **(payload or {}),
    }, status=status or (200 if success else 400))


def _validate_upload_file(uploaded_file):
    if not uploaded_file:
        return ''
    extension = Path(uploaded_file.name).suffix.lower()
    if uploaded_file.size and uploaded_file.size > MAX_UPLOAD_IMAGE_BYTES:
        max_mb = MAX_UPLOAD_IMAGE_BYTES // (1024 * 1024)
        raise ValueError(f'Image files must be {max_mb}MB or smaller.')
    if extension not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError('Only JPG, JPEG, PNG, and WEBP images are supported.')
    return extension


def _content_type_for_extension(extension, uploaded_file):
    provided_type = (getattr(uploaded_file, 'content_type', '') or '').strip().lower()
    if provided_type in {'image/jpeg', 'image/png', 'image/webp'}:
        return provided_type
    return {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.webp': 'image/webp',
    }.get(extension, 'application/octet-stream')


def _save_uploaded_image(uploaded_file, folder, base_name):
    if not uploaded_file:
        return None

    extension = _validate_upload_file(uploaded_file)
    safe_name = slugify(base_name) or 'image'
    token = uuid4().hex[:12]
    filename = f"{safe_name}-{token}{extension}"
    stored_path = (Path(DB_UPLOAD_PREFIX) / folder / filename).as_posix()
    image_bytes = b''.join(uploaded_file.chunks())

    UploadedImage.objects.create(
        path=stored_path,
        original_name=Path(uploaded_file.name).name[:255],
        content_type=_content_type_for_extension(extension, uploaded_file),
        data=image_bytes,
        size=len(image_bytes),
    )
    return stored_path
def _save_uploaded_product_image(uploaded_file, product_name, slot_name):
    return _save_uploaded_image(uploaded_file, 'products', f'{product_name}-{slot_name}')


def _save_uploaded_asset_image(uploaded_file, asset_name):
    return _save_uploaded_image(uploaded_file, 'assets', asset_name)


def _is_uploaded_asset_path(image_path):
    normalized = Path((image_path or '').replace('\\', '/').lstrip('/'))
    return bool(normalized.parts[:1] in UPLOAD_PATH_PREFIXES and '..' not in normalized.parts and not normalized.is_absolute())


def _uploaded_asset_is_referenced(image_path):
    normalized = _normalize_image_asset_path(image_path)
    if not normalized or not _is_uploaded_asset_path(normalized):
        return True
    variant_filter = (
        Q(image1=normalized) | Q(image2=normalized) |
        Q(image3=normalized) | Q(image4=normalized)
    )
    return any([
        ProductVariant.objects.filter(variant_filter).exists(),
        FashionCategory.objects.filter(Q(image=normalized) | Q(banner_image=normalized)).exists(),
        SiteAsset.objects.filter(image_path=normalized).exists(),
        PageAsset.objects.filter(image_path=normalized).exists(),
    ])


def _delete_uploaded_asset_if_unused(image_path):
    normalized = _normalize_image_asset_path(image_path)
    if not normalized or not _is_uploaded_asset_path(normalized) or _uploaded_asset_is_referenced(normalized):
        return False

    normalized_path = Path(normalized)
    if normalized_path.parts[:1] == (DB_UPLOAD_PREFIX,):
        deleted_count, _ = UploadedImage.objects.filter(path=normalized).delete()
        return bool(deleted_count)

    file_path = Path(settings.MEDIA_ROOT) / normalized_path
    try:
        file_path.relative_to(Path(settings.MEDIA_ROOT) / 'uploads')
    except ValueError:
        return False
    if file_path.exists():
        file_path.unlink()
        return True
    return False
def _delete_replaced_uploaded_assets(old_paths, new_paths):
    new_path_set = {_normalize_image_asset_path(path) for path in new_paths if path}
    for old_path in old_paths:
        normalized = _normalize_image_asset_path(old_path)
        if normalized and normalized not in new_path_set:
            _delete_uploaded_asset_if_unused(normalized)

def _is_forbidden_image_reference(raw_path):
    value = (raw_path or '').strip()
    lowered = value.lower()
    return bool(
        lowered.startswith(('blob:', 'file:', 'filesystem:'))
        or lowered.startswith(('http://localhost', 'https://localhost', 'http://127.0.0.1', 'https://127.0.0.1'))
        or lowered.startswith('c:/')
        or lowered.startswith('c:\\')
        or lowered.startswith('\\\\')
        or ':\\' in lowered
    )


def _normalize_admin_asset_path(raw_path):
    if _is_forbidden_image_reference(raw_path):
        return ''
    normalized = (raw_path or '').strip().replace('\\', '/').lstrip('/')
    if not normalized:
        return ''
    if normalized.startswith('static/'):
        normalized = normalized[len('static/'):]
    path = Path(normalized)
    if path.is_absolute() or '..' in path.parts:
        return ''
    if path.parts[:1] not in {('images',), ('uploads',), (DB_UPLOAD_PREFIX,)}:
        if '/' in normalized:
            return ''
        normalized = f'images/{normalized}'
    return normalized

def _get_background_asset_record(choice):
    if choice['scope'] == 'site':
        return SiteAsset.objects.filter(asset_key=choice['asset_key']).first()
    return PageAsset.objects.filter(
        page_key=choice['page_key'],
        asset_key=choice['asset_key'],
    ).first()


def _save_background_asset_record(choice, image_path, is_active):
    if choice['scope'] == 'site':
        asset = SiteAsset.objects.filter(asset_key=choice['asset_key']).first()
        if asset is None:
            asset = SiteAsset(asset_key=choice['asset_key'])
    else:
        asset = PageAsset.objects.filter(
            page_key=choice['page_key'],
            asset_key=choice['asset_key'],
        ).first()
        if asset is None:
            asset = PageAsset(page_key=choice['page_key'], asset_key=choice['asset_key'])

    asset.image_path = image_path
    asset.is_active = is_active
    asset.save()
    return asset


def _delete_background_file_if_unused(image_path, fallback_path):
    normalized = _normalize_image_asset_path(image_path)
    if not normalized or normalized == _normalize_image_asset_path(fallback_path):
        return False
    return _delete_uploaded_asset_if_unused(normalized)


def _next_fashion_sort_order(root_category, parent, exclude_category_id=None):
    siblings = FashionCategory.objects.filter(root_category=root_category, parent=parent)
    if exclude_category_id:
        siblings = siblings.exclude(fashion_category_id=exclude_category_id)
    max_order = siblings.aggregate(max_order=Max('sort_order'))['max_order']
    return (max_order or 0) + 1


def _unique_fashion_slug(name, parent, category_id=None):
    base_slug = slugify(name)[:150] or 'fashion-category'
    slug = base_slug
    counter = 2
    while FashionCategory.objects.filter(parent=parent, slug=slug).exclude(fashion_category_id=category_id).exists():
        suffix = f'-{counter}'
        slug = f'{base_slug[:160 - len(suffix)]}{suffix}'
        counter += 1
    return slug


def admin_product_form(request, product_id=None):
    access_redirect = _require_shop_admin(request)
    if access_redirect:
        return access_redirect

    product = Product.objects.filter(product_id=product_id).first() if product_id else None
    variant = ProductVariant.objects.filter(product=product).first() if product else None
    existing_variants = list(ProductVariant.objects.filter(product=product).order_by('variant_id')) if product else []
    categories = Category.objects.order_by('category_name')
    subcategories = SubCategory.objects.select_related('category').order_by('subcategory_name')
    fashion_categories = FashionCategory.objects.filter(is_active=True, level__gte=2).select_related('root_category', 'parent').order_by('root_category__category_name', 'level', 'sort_order', 'fashion_category_id')

    if request.method == 'POST':
        product_name = (request.POST.get('product_name') or '').strip()
        brand = (request.POST.get('brand') or '').strip()
        category_id = request.POST.get('category_id')
        subcategory_id = request.POST.get('subcategory_id')
        fashion_category_id = request.POST.get('fashion_category_id')
        sku = (request.POST.get('sku') or '').strip()
        price = _parse_decimal(request.POST.get('price'))
        discount_percent = _parse_decimal(request.POST.get('discount_percent'))
        description = (request.POST.get('description') or '').strip()
        size = (request.POST.get('size') or '').strip()
        size_quantities_raw = (request.POST.get('size_quantities') or '').strip()
        color = (request.POST.get('color') or '').strip()
        quantity = max(0, int(request.POST.get('quantity') or 0))
        image1 = (request.POST.get('image1') or '').strip() or None
        image2 = (request.POST.get('image2') or '').strip() or None
        image3 = (request.POST.get('image3') or '').strip() or None
        image4 = (request.POST.get('image4') or '').strip() or None
        image1_upload = request.FILES.get('image1_upload')
        image2_upload = request.FILES.get('image2_upload')
        image3_upload = request.FILES.get('image3_upload')
        image4_upload = request.FILES.get('image4_upload')
        is_active = request.POST.get('is_active') == 'on'
        is_featured = request.POST.get('is_featured') == 'on'

        category = Category.objects.filter(category_id=category_id).first()
        subcategory = SubCategory.objects.filter(subcategory_id=subcategory_id).first()
        fashion_category = FashionCategory.objects.filter(fashion_category_id=fashion_category_id).select_related('root_category', 'parent').first()
        if fashion_category and fashion_category.children.filter(is_active=True).exists():
            response = _json_admin_response(request, False, 'Choose a sub category leaf for the product, not a gender or main category.')
            if response:
                return response
            messages.error(request, 'Choose a sub category leaf for the product, not a gender or main category.')
            return redirect(_safe_redirect_target(request, request.POST.get('next'), 'admin_control_center'))
        if fashion_category and fashion_category.root_category:
            category = fashion_category.root_category
            subcategory, _ = SubCategory.objects.get_or_create(
                category=category,
                subcategory_name=fashion_category.name,
            )
        if not all([product_name, category, subcategory, sku]):
            response = _json_admin_response(request, False, 'Product name, category/subcategory or fashion category, and SKU are required.')
            if response:
                return response
            messages.error(request, 'Product name, category/subcategory or fashion category, and SKU are required.')
        else:
            if product is None:
                product = Product()

            product.product_name = product_name
            product.brand = brand or None
            product.category = category
            product.subcategory = subcategory
            product.fashion_category = fashion_category
            product.sku = sku
            product.price = price
            product.discount_percent = discount_percent
            product.description = description or None
            product.is_active = is_active
            product.is_featured = is_featured
            product.save()

            old_image_paths = []
            for old_variant in existing_variants:
                old_image_paths.extend([old_variant.image1, old_variant.image2, old_variant.image3, old_variant.image4])

            if variant is None:
                variant = ProductVariant(product=product)

            try:
                uploaded_image1_name = _save_uploaded_product_image(image1_upload, product.product_name, 'image1')
                uploaded_image2_name = _save_uploaded_product_image(image2_upload, product.product_name, 'image2')
                uploaded_image3_name = _save_uploaded_product_image(image3_upload, product.product_name, 'image3')
                uploaded_image4_name = _save_uploaded_product_image(image4_upload, product.product_name, 'image4')
            except ValueError as exc:
                response = _json_admin_response(request, False, str(exc))
                if response:
                    return response
                messages.error(request, str(exc))
                return redirect(_safe_redirect_target(request, request.POST.get('next'), 'admin_control_center'))

            final_image1 = uploaded_image1_name or image1
            final_image2 = uploaded_image2_name or image2
            final_image3 = uploaded_image3_name or image3
            final_image4 = uploaded_image4_name or image4
            size_variants = _parse_size_quantities(size_quantities_raw)

            if size_variants:
                ProductVariant.objects.filter(product=product).delete()
                for variant_size, variant_quantity in size_variants:
                    ProductVariant.objects.create(
                        product=product,
                        size=variant_size,
                        color=color or None,
                        quantity=variant_quantity,
                        image1=final_image1,
                        image2=final_image2,
                        image3=final_image3,
                        image4=final_image4,
                    )
            else:
                variant.size = size or None
                variant.color = color or None
                variant.quantity = quantity
                variant.image1 = final_image1
                variant.image2 = final_image2
                variant.image3 = final_image3
                variant.image4 = final_image4
                variant.save()

            _delete_replaced_uploaded_assets(old_image_paths, [final_image1, final_image2, final_image3, final_image4])
            response = _json_admin_response(request, True, 'Product saved successfully.', {
                'product': _admin_product_payload(product),
            })
            if response:
                return response
            messages.success(request, 'Product saved successfully.')
            return redirect(_safe_redirect_target(request, request.POST.get('next'), 'admin_control_center'))

    return render(request, 'store/upload.html', {
        'product': product,
        'variant': variant,
        'size_quantities_value': '\n'.join(
            f"{item.size}: {item.quantity}" for item in existing_variants if item.size
        ),
        'categories': categories,
        'subcategories': subcategories,
        'fashion_categories': fashion_categories,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'page_title': 'Edit Product' if product else 'Add Product',
        'form_action_label': 'Update Product' if product else 'Add Product',
    })


def admin_product_delete(request, product_id):
    access_redirect = _require_shop_admin(request)
    if access_redirect:
        return access_redirect

    product = get_object_or_404(Product, product_id=product_id)
    if request.method == 'POST':
        old_paths = []
        for old_variant in ProductVariant.objects.filter(product=product):
            old_paths.extend([old_variant.image1, old_variant.image2, old_variant.image3, old_variant.image4])
        product.delete()
        _delete_replaced_uploaded_assets(old_paths, [])
        response = _json_admin_response(request, True, 'Product deleted successfully.', {'deleted_id': product_id})
        if response:
            return response
        messages.success(request, 'Product deleted successfully.')
    return redirect(_safe_redirect_target(request, request.POST.get('next'), 'admin_control_center'))


def admin_orders(request):
    access_redirect = _require_shop_admin(request)
    if access_redirect:
        return access_redirect

    orders_qs = Order.objects.select_related('user').prefetch_related('items__product')
    status_filter = (request.GET.get('status') or '').strip()
    if status_filter:
        orders_qs = orders_qs.filter(status__iexact=status_filter)
    return render(request, 'store/admin-orders.html', {
        'orders': orders_qs,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
    })


def admin_order_update(request, order_id):
    access_redirect = _require_shop_admin(request)
    if access_redirect:
        return access_redirect

    order = get_object_or_404(Order, order_id=order_id)
    if request.method == 'POST':
        order.status = (request.POST.get('status') or order.status).strip()
        order.payment_status = (request.POST.get('payment_status') or order.payment_status).strip()
        order.courier_name = (request.POST.get('courier_name') or '').strip() or None
        order.tracking_number = (request.POST.get('tracking_number') or '').strip() or None
        order.tracking_url = (request.POST.get('tracking_url') or '').strip() or None
        estimated_delivery = (request.POST.get('estimated_delivery') or '').strip()
        order.estimated_delivery = estimated_delivery or None
        order.save(update_fields=['status', 'payment_status', 'courier_name', 'tracking_number', 'tracking_url', 'estimated_delivery'])
        messages.success(request, f'Order #{order.order_id} updated successfully.')
    return redirect(_safe_redirect_target(request, request.POST.get('next'), 'admin_control_center'))


def admin_customers(request):
    access_redirect = _require_shop_admin(request)
    if access_redirect:
        return access_redirect

    customers = User.objects.filter(is_staff=False, is_superuser=False).order_by('-date_joined')
    rows = []
    for customer in customers:
        orders_qs = Order.objects.filter(user=customer)
        rows.append({
            'customer': customer,
            'order_count': orders_qs.count(),
            'total_spent': orders_qs.filter(payment_status__in=['Paid', 'Authorized', 'Captured']).aggregate(total=Sum('subtotal'))['total'] or Decimal('0'),
            'latest_order': orders_qs.first(),
        })
    return render(request, 'store/admin-customers.html', {
        'customers': rows,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
    })


def admin_customer_detail(request, user_id):
    access_redirect = _require_shop_admin(request)
    if access_redirect:
        return access_redirect

    customer = get_object_or_404(User, pk=user_id)
    return render(request, 'store/admin-customer-detail.html', {
        'customer': customer,
        'orders': Order.objects.filter(user=customer).prefetch_related('items__product'),
        'addresses': UserAddress.objects.filter(user=customer),
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
    })


def admin_inventory(request):
    access_redirect = _require_shop_admin(request)
    if access_redirect:
        return access_redirect

    variants = ProductVariant.objects.select_related('product', 'product__category', 'product__subcategory').order_by('quantity', 'product__product_name')
    return render(request, 'store/admin-inventory.html', {
        'variants': variants,
        'low_stock_threshold': 5,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
    })


def _admin_category_payload(category):
    return {
        'id': category.fashion_category_id,
        'name': category.name,
        'slug': category.slug,
        'full_path': category.full_path,
        'root_category_id': category.root_category_id,
        'parent_id': category.parent_id,
        'level': category.level,
        'sort_order': category.sort_order,
        'description': category.description or '',
        'page_url': category.page_url or '',
        'image': category.image or '',
        'image_url': _asset_public_url(category.image) if category.image else '',
        'banner_image': category.banner_image or '',
        'banner_image_url': _asset_public_url(category.banner_image) if category.banner_image else '',
        'is_active': category.is_active,
        'is_under_maintenance': category.is_under_maintenance,
    }


def _admin_category_list_payload():
    categories = FashionCategory.objects.select_related('root_category', 'parent').order_by(
        'root_category__category_name', 'level', 'sort_order', 'fashion_category_id'
    )
    return [_admin_category_payload(category) for category in categories]


def _delete_admin_category(category_id):
    if not category_id:
        return False, 'Category id is required.', {}, 400

    category = FashionCategory.objects.filter(fashion_category_id=category_id).first()
    if category is None:
        return False, 'Category card was not found.', {}, 404

    category_name = category.name
    old_paths = [category.image, category.banner_image]
    deleted_id = category.fashion_category_id
    category.delete()
    _delete_replaced_uploaded_assets(old_paths, [])
    return True, f'"{category_name}" category card deleted successfully.', {'deleted_id': deleted_id}, 200


def _is_descendant_category(candidate_parent, category):
    current = candidate_parent
    while current:
        if current.fashion_category_id == category.fashion_category_id:
            return True
        current = current.parent
    return False


def _save_admin_category_from_request(request):
    action = (request.POST.get('action') or 'save_fashion_category').strip()
    fashion_category_id = request.POST.get('fashion_category_id')

    if action == 'delete_fashion_category':
        return _delete_admin_category(fashion_category_id)

    name = (request.POST.get('name') or '').strip()
    root_category_id = request.POST.get('root_category_id')
    parent_id = request.POST.get('parent_id')
    raw_sort_order = request.POST.get('sort_order')
    sort_order = None if raw_sort_order in (None, '') else max(0, _parse_int(raw_sort_order, 0))
    description = (request.POST.get('description') or '').strip()
    page_url = (request.POST.get('page_url') or '').strip()
    raw_image_input = request.POST.get('image')
    image_path = _normalize_admin_asset_path(raw_image_input)
    raw_banner_image_input = request.POST.get('banner_image')
    banner_image_path = _normalize_admin_asset_path(raw_banner_image_input)
    is_under_maintenance = request.POST.get('is_under_maintenance') == 'on'
    is_active = request.POST.get('is_active') == 'on'

    if not name:
        return False, 'Category name is required.', {}, 400

    root_category = Category.objects.filter(category_id=root_category_id).first()
    if root_category is None:
        return False, 'Root category is required.', {}, 400

    category = FashionCategory.objects.filter(fashion_category_id=fashion_category_id).first() if fashion_category_id else None
    if fashion_category_id and category is None:
        return False, 'Category card was not found.', {}, 404

    parent = None
    if parent_id:
        parent = FashionCategory.objects.filter(fashion_category_id=parent_id).first()
        if parent is None:
            return False, 'Parent category was not found.', {}, 400
        if category and _is_descendant_category(parent, category):
            return False, 'A category cannot be moved under itself or its child category.', {}, 400

    try:
        uploaded_image_path = _save_uploaded_asset_image(request.FILES.get('image_upload'), name)
        uploaded_banner_path = _save_uploaded_asset_image(request.FILES.get('banner_upload'), f'{name}-banner')
    except ValueError as exc:
        return False, str(exc), {}, 400

    if category is None:
        category = FashionCategory()

    preserve_existing_image = raw_image_input is None and category.fashion_category_id and not _is_forbidden_image_reference(category.image)
    final_image_path = uploaded_image_path or image_path or (category.image if preserve_existing_image else '')
    preserve_existing_banner = raw_banner_image_input is None and category.fashion_category_id and not _is_forbidden_image_reference(category.banner_image)
    final_banner_path = uploaded_banner_path or banner_image_path or (category.banner_image if preserve_existing_banner else '')
    previous_paths = [category.image, category.banner_image]

    category.name = name
    category.slug = _unique_fashion_slug(name, parent, category.fashion_category_id)
    category.root_category = root_category
    category.parent = parent
    category.level = (parent.level + 1) if parent else 0
    category.sort_order = sort_order if sort_order is not None else _next_fashion_sort_order(root_category, parent, category.fashion_category_id)
    category.image = final_image_path or None
    category.banner_image = final_banner_path or None
    category.description = description or None
    category.page_url = page_url or None
    category.is_active = is_active
    category.is_under_maintenance = is_under_maintenance
    category.save()
    _delete_replaced_uploaded_assets(previous_paths, [category.image, category.banner_image])

    if parent:
        SubCategory.objects.get_or_create(category=root_category, subcategory_name=name)

    return True, 'Operation completed successfully.', {'category': _admin_category_payload(category)}, 200

def admin_categories(request):
    access_redirect = _require_shop_admin(request)
    if access_redirect:
        return access_redirect

    if request.method == 'POST':
        success, message, payload, status = _save_admin_category_from_request(request)
        response = _json_admin_response(request, success, message, payload, status=status)
        if response:
            return response
        if success:
            messages.success(request, message)
        else:
            messages.error(request, message)
        return redirect(_safe_redirect_target(request, request.POST.get('next'), 'admin_control_center'))
    fashion_categories = FashionCategory.objects.select_related('root_category', 'parent').order_by('root_category__category_name', 'level', 'sort_order', 'fashion_category_id')
    root_nodes = FashionCategory.objects.filter(parent__isnull=True).select_related('root_category').order_by('sort_order', 'fashion_category_id')
    card_groups = []
    for root_node in root_nodes:
        cards = FashionCategory.objects.filter(root_category=root_node.root_category).exclude(fashion_category_id=root_node.fashion_category_id).select_related('root_category', 'parent').order_by('level', 'sort_order', 'fashion_category_id')
        card_groups.append({
            'root': root_node,
            'cards': cards,
        })

    return render(request, 'store/admin-categories.html', {
        'categories': Category.objects.order_by('category_name'),
        'fashion_categories': fashion_categories,
        'card_groups': card_groups,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
    })


def admin_category_api(request, fashion_category_id=None):
    access_redirect = _require_shop_admin(request)
    if access_redirect:
        return access_redirect

    if request.method == 'GET':
        if fashion_category_id:
            category = FashionCategory.objects.select_related('root_category', 'parent').filter(
                fashion_category_id=fashion_category_id
            ).first()
            if category is None:
                return JsonResponse({
                    'success': False,
                    'message': 'Category card was not found.',
                }, status=404)
            return JsonResponse({
                'success': True,
                'message': 'Operation completed successfully',
                'category': _admin_category_payload(category),
            })
        return JsonResponse({
            'success': True,
            'message': 'Operation completed successfully',
            'categories': _admin_category_list_payload(),
        })

    if request.method == 'DELETE':
        success, message, payload, status = _delete_admin_category(fashion_category_id or request.GET.get('fashion_category_id'))
        return JsonResponse({
            'success': success,
            'message': 'Operation completed successfully' if success else message,
            **payload,
        }, status=status)

    if request.method == 'POST':
        success, message, payload, status = _save_admin_category_from_request(request)
        return JsonResponse({
            'success': success,
            'message': 'Operation completed successfully' if success else message,
            **payload,
        }, status=status)

    return JsonResponse({
        'success': False,
        'message': 'Method not allowed.',
    }, status=405)

def admin_backgrounds(request):
    access_redirect = _require_shop_admin(request)
    if access_redirect:
        return access_redirect

    if request.method == 'POST':
        action = (request.POST.get('action') or 'save_background').strip()
        choice_id = request.POST.get('choice_id')
        selected_choice = None
        for choice in BACKGROUND_ASSET_CHOICES:
            current_id = f"{choice['scope']}:{choice['page_key']}:{choice['asset_key']}"
            if current_id == choice_id:
                selected_choice = choice
                break

        if selected_choice is None:
            messages.error(request, 'Choose a valid background section to update.')
            return redirect(_safe_redirect_target(request, request.POST.get('next'), 'admin_control_center'))

        if action == 'delete_background':
            asset = _get_background_asset_record(selected_choice)
            previous_path = getattr(asset, 'image_path', '') or ''
            if asset:
                asset.image_path = selected_choice['fallback']
                asset.is_active = True
                asset.save()
            deleted = _delete_background_file_if_unused(previous_path, selected_choice['fallback'])
            message_suffix = ' The uploaded file was removed.' if deleted else ''
            message = f"{selected_choice['label']} reset to its default background.{message_suffix}"
            response = _json_admin_response(request, True, message, {
                'background': {
                    'choice_id': choice_id,
                    'path': selected_choice['fallback'],
                    'url': _asset_public_url(selected_choice['fallback']),
                }
            })
            if response:
                return response
            messages.success(request, message)
            return redirect(_safe_redirect_target(request, request.POST.get('next'), 'admin_control_center'))

        try:
            uploaded_path = _save_uploaded_asset_image(
                request.FILES.get('background_upload'),
                f"{selected_choice['page_key'] or 'site'}-{selected_choice['asset_key']}",
            )
        except ValueError as exc:
            response = _json_admin_response(request, False, str(exc))
            if response:
                return response
            messages.error(request, str(exc))
            return redirect(_safe_redirect_target(request, request.POST.get('next'), 'admin_control_center'))
        typed_path = _normalize_admin_asset_path(request.POST.get('image_path'))
        final_path = uploaded_path or typed_path or selected_choice['fallback']

        if not _asset_exists(final_path):
            response = _json_admin_response(request, False, 'That image was not found.')
            if response:
                return response
            messages.error(request, 'That image was not found.')
            return redirect(_safe_redirect_target(request, request.POST.get('next'), 'admin_control_center'))

        previous_asset = _get_background_asset_record(selected_choice)
        previous_path = getattr(previous_asset, 'image_path', '') or ''
        _save_background_asset_record(
            selected_choice,
            final_path,
            request.POST.get('is_active') == 'on',
        )
        _delete_replaced_uploaded_assets([previous_path], [final_path])
        message = f"{selected_choice['label']} updated successfully."
        response = _json_admin_response(request, True, message, {
            'background': {
                'choice_id': choice_id,
                'path': final_path,
                'url': _asset_public_url(final_path),
            }
        })
        if response:
            return response
        messages.success(request, message)
        return redirect(_safe_redirect_target(request, request.POST.get('next'), 'admin_control_center'))

    background_rows = []
    for choice in BACKGROUND_ASSET_CHOICES:
        try:
            asset = _get_background_asset_record(choice)
        except (ProgrammingError, OperationalError):
            asset = None

        saved_path = getattr(asset, 'image_path', '') or ''
        if choice['scope'] == 'site':
            current_path = _get_site_asset(choice['asset_key'], choice['fallback'])
        else:
            current_path = _get_page_asset(choice['page_key'], choice['asset_key'], choice['fallback'])

        background_rows.append({
            **choice,
            'choice_id': f"{choice['scope']}:{choice['page_key']}:{choice['asset_key']}",
            'saved_path': saved_path,
            'current_path': current_path,
            'current_url': _asset_public_url(current_path),
            'is_active': getattr(asset, 'is_active', True),
        })

    return render(request, 'store/admin-backgrounds.html', {
        'backgrounds': background_rows,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
    })


def mens_shirts(request):
    
    products = Product.objects.filter(
        category__category_name__iexact='mens',
        subcategory__subcategory_name__iexact='shirts',
        is_active=True,
    ).select_related('category', 'subcategory').prefetch_related('productvariant_set')

    image_cards = []

    for product in products:
        variant = _get_first_variant(product)
        image_names = [variant.image1, variant.image2, variant.image3, variant.image4] if variant else []

        valid_images = []
        for image_name in image_names:
            if not image_name:
                continue
            image_path = Path(settings.BASE_DIR) / 'static' / 'images' / image_name
            if image_path.exists():
                valid_images.append(f'images/{image_name}')

        if not valid_images:
            valid_images = [_get_product_image_path(product, variant)]

        primary_image = valid_images[0] if valid_images else _get_product_image_path(product, variant)
        image_cards.append({
            'product_id': product.product_id,
            'product_name': product.product_name,
            'brand': product.brand,
            'offer_price': product.offer_price,
            'image_path': primary_image,
            'card_name': product.product_name,
        })

    page_background_image = _get_page_asset('mens_shirts', 'background', 'images/shirt.jpg')
    return render(request, 'store/mens-shirts.html', {
        'image_cards': image_cards,
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'page_background_image': page_background_image,
        'page_background_url': _asset_public_url(page_background_image),
    })

# Optional: Django REST Framework API
# Only add this if you installed DRF
from rest_framework import viewsets
from .serializers import ProductSerializer  # you need this serializer

class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Product.objects.filter(is_active=True).select_related('category', 'subcategory', 'fashion_category')
    serializer_class = ProductSerializer 


def under_maintenance_view(request):
    return render(request, 'store/under-maintenance.html', {
        'page_title': 'Under Maintenance',
        'status_message': 'This section is currently under maintenance. Please check back later.',
        'logo_image': _get_site_asset('logo', 'images/logo1.webp'),
        'category_return_url': '/',
    })

