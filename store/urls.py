from django.urls import path, include
from . import views

# Optional: DRF API router
from rest_framework import routers

router = routers.DefaultRouter()
# Only enable this if ProductViewSet exists
router.register('products', views.ProductViewSet)

urlpatterns = [
    path('assets/<path:asset_path>', views.store_asset, name='store_asset'),

    # DRF API
    path('api/', include(router.urls)),     
    path('api/categories/', views.admin_category_api, name='admin_category_api'),
    path('api/categories/<int:fashion_category_id>/', views.admin_category_api, name='admin_category_detail_api'),

    # Home page
    path('', views.home, name='home'),
  # Root category pages
    path(
        'mens/',
        views.category_view,
        {'category_name': 'mens'},
        name='mens_root'
    ),
    path(
        'men/',
        views.category_view,
        {'category_name': 'mens'},
        name='men_root'
    ),

    path(
        'womens/',
        views.category_view,
        {'category_name': 'womens'},
        name='womens_root'
    ),
    path(
        'women/',
        views.category_view,
        {'category_name': 'womens'},
        name='women_root_legacy'
    ),

path(
    'kids/',
    views.category_view,
    {'category_name': 'kids'},
    name='kids_root'
),

# Shared accessories
path(
    'accessories/common/',
    views.shared_accessories,
    name='shared_accessories'
),

# Men product pages
path(
    'mens/<path:category_path>/<slug:product_slug>/',
    views.fashion_product_slug_detail,
    {'gender_slug': 'mens'},
    name='mens_fashion_product_slug_detail'
),

# Men category pages
path(
    'mens/<path:category_path>/',
    views.fashion_category_slug_listing,
    {'gender_slug': 'mens'},
    name='mens_fashion_category_slug_listing'
),

# Women product pages
path(
    'womens/<path:category_path>/<slug:product_slug>/',
    views.fashion_product_slug_detail,
    {'gender_slug': 'womens'},
    name='womens_fashion_product_slug_detail'
),

# Women category pages
path(
    'womens/<path:category_path>/',
    views.fashion_category_slug_listing,
    {'gender_slug': 'womens'},
    name='womens_fashion_category_slug_listing'
),

# Kids product pages
path(
    'kids/<path:category_path>/<slug:product_slug>/',
    views.fashion_product_slug_detail,
    {'gender_slug': 'kids'},
    name='kids_fashion_product_slug_detail'
),

# Kids category pages
path(
    'kids/<path:category_path>/',
    views.fashion_category_slug_listing,
    {'gender_slug': 'kids'},
    name='kids_fashion_category_slug_listing'
),
    path('accessories/common/', views.shared_accessories, name='shared_accessories'),
    path('contact-us/', views.contact_us, name='contact_us'),
    path('contact.html', views.contact_us, name='contact_html'),
    path('privacy-policy/', views.privacy_policy, name='privacy_policy'),
    path('refund-policy/', views.refund_policy, name='refund_policy'),
    path('terms-and-conditions/', views.terms_and_conditions, name='terms_and_conditions'),
    path('search/', views.search_results, name='search_results'),
    path('catalog/', views.fashion_catalog, name='fashion_catalog'),
    path('catalog/category/<int:category_id>/', views.fashion_category_listing, name='fashion_category_listing'),
    
    path('mens_tshirts', views.mens_tshirts, name='mens_tshirts_legacy'),
    path('mens_tshirts/', views.mens_tshirts, name='mens_tshirts'),
    path('mens_tshirts/<int:product_id>/', views.mens_tshirt_detail, name='mens_tshirt_detail'),
    path('mens_shirts/', views.mens_shirts, name='mens_shirts'),
    path('mens_jeans/', views.mens_jeans, name='mens_jeans'),
    path('mens_jeans/<int:product_id>/', views.mens_jeans_detail, name='mens_jeans_detail'),
    path('accessories_men/', views.accessories_men, name='accessories_men'),
    path('accessories_women/', views.accessories_women, name='accessories_women'),
    path('womens_ethnicware/', views.womens_ethnicware, name='womens_ethnicware'),
    path('womens_westernware/', views.womens_westernware, name='womens_westernware'),
    path('womens_footwear/', views.womens_footwear, name='womens_footwear'),
    path('kids_tshirts/', views.kids_tshirts, name='kids_tshirts'),
    path('kids_dresses/', views.kids_dresses, name='kids_dresses'),
    path('kids_toys/', views.kids_toys, name='kids_toys'),
    path('kids_footwear/', views.kids_footwear, name='kids_footwear'),
    path('catalog/product/<int:product_id>/', views.catalog_product_detail, name='catalog_product_detail'),
    path('mens_shirt1_detailed/', views.product_detail, {'product_id': 27}, name='mens_shirt1_detailed'),
    
    # Login and Cart
    path('login/', views.login_view, name='login'),
    path('register/', views.register_view, name='register'),
    path('logout/', views.logout_view, name='logout'),
    path('admin-access/', views.admin_access, name='admin_access'),
    path('admin_panel', views.admin_control_center, name='admin_control_center'),
    path('admin-panel/', views.admin_dashboard, name='admin_dashboard'),
    path('admin-panel/backgrounds/', views.admin_backgrounds, name='admin_backgrounds'),
    path('admin-panel/orders/', views.admin_orders, name='admin_orders'),
    path('admin-panel/orders/<int:order_id>/update/', views.admin_order_update, name='admin_order_update'),
    path('admin-panel/customers/', views.admin_customers, name='admin_customers'),
    path('admin-panel/customers/<int:user_id>/', views.admin_customer_detail, name='admin_customer_detail'),
    path('admin-panel/inventory/', views.admin_inventory, name='admin_inventory'),
    path('admin-panel/categories/', views.admin_categories, name='admin_categories'),
    path('admin-panel/products/add/', views.admin_product_form, name='admin_product_add'),
    path('admin-panel/products/<int:product_id>/edit/', views.admin_product_form, name='admin_product_edit'),
    path('admin-panel/products/<int:product_id>/delete/', views.admin_product_delete, name='admin_product_delete'),
    path('cart/', views.cart, name='cart'),
    path('payment/', views.payment_gateway, name='payment_gateway'),
    path('payment/start/', views.start_payment_gateway, name='start_payment_gateway'),
    path('payment/create/', views.create_razorpay_checkout, name='create_razorpay_checkout'),
    path('payment/verify/', views.verify_razorpay_payment, name='verify_razorpay_payment'),
    path('payment/webhook/', views.razorpay_webhook, name='razorpay_webhook'),
    path('payment/callback/', views.razorpay_payment_link_callback, name='razorpay_payment_link_callback'),
    path('place-order/', views.place_order, name='place_order'),
    path('orders/', views.orders, name='orders'),
    path('orders/<int:order_id>/success/', views.order_success, name='order_success'),
    path('orders/<int:order_id>/tracking/', views.order_tracking, name='order_tracking'),
    path('orders/<int:order_id>/tracking-api/', views.order_tracking_api, name='order_tracking_api'),
    path('cart/add/<int:product_id>/', views.add_to_cart, name='add_to_cart'),
    path('cart/remove/<int:cart_item_id>/', views.remove_from_cart, name='remove_from_cart'),
    path('cart/remove/session/<path:item_key>/', views.remove_session_cart_item, name='remove_session_cart_item'),
    path('cart/update/<int:cart_item_id>/', views.update_cart_item, name='update_cart_item'),
    path('cart/update/session/<path:item_key>/', views.update_session_cart_item, name='update_session_cart_item'),
    path('buy-now/<int:product_id>/', views.buy_now, name='buy_now'),
    path('wishlist/', views.wishlist, name='wishlist'),
    path('wishlist/toggle/<int:product_id>/', views.toggle_wishlist, name='toggle_wishlist'),
    path('dashboard/', views.user_dashboard, name='user_dashboard'),
    path('dashboard/profile/', views.profile_settings, name='profile_settings'),
    path('dashboard/addresses/', views.address_book, name='address_book'),

    # Dynamic category pages
    path('category/<str:category_name>/', views.category_view, name='category'),
    

    # Shortcut URLs for Men/Women/Kids
    path('category_men/', views.category_view, {'category_name': 'men'}, name='category_men'),
    path('category_women/', views.category_view, {'category_name': 'women'}, name='category_women'),
    path('category_kids/', views.category_view, {'category_name': 'kids'}, name='category_kids'),

    # Dynamic product detail
    path('product/<int:product_id>/', views.product_detail, name='product_detail'),
]

urlpatterns += [
    path('health/', views.health_check, name='health_check'),
    path('under-maintenance/', views.under_maintenance_view, name='under_maintenance_view'),
]
