from django.urls import path
from django.contrib.auth import views as auth_views

from . import views
from . import chatbot_views

urlpatterns = [
    path("planning/pending-list/", views.planning_pending_list_view, name="planning_pending_list"),

    path("", views.landing, name="landing"),
    path("login/", views.custom_login, name="login"),
    path("accounts/login/", views.custom_login, name="accounts_login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),

    path("admin-dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path("user-dashboard/", views.user_dashboard, name="user_dashboard"),
    path("store-dashboard/", views.store_dashboard, name="store_dashboard"),
    path("stock/manage/", views.stock_manage, name="stock_manage"),
    path("super-admin-panel/", views.super_admin_panel, name="super_admin_panel"),
    path("add-die-register/", views.add_die_register, name="add_die_register"),
    path("die-registers/", views.die_register_list, name="die_register_list"),
    path("materials-customers/", views.materials_customers, name="materials_customers"),
    path("accounts-dashboard/", views.accounts_dashboard, name="accounts_dashboard"),
    path("accounts/new-purchase-order/", views.new_purchase_order, name="new_purchase_order"),
    path("accounts/po-list/", views.purchase_order_list, name="purchase_order_list"),
    path("accounts/po/<int:pk>/update-status/", views.update_po_status, name="update_po_status"),
    path("planning/product-mapping/", views.product_mapping, name="product_mapping"),
    path("jobcards/", views.jobcard_list, name="jobcard_list"),
    path("jobcard-status/", views.jobcard_status, name="jobcard_status"),
    path("jobcard/<int:pk>/", views.jobcard_detail, name="jobcard_detail"),
    path("jobcard/<int:pk>/pdf/", views.jobcard_pdf, name="jobcard_pdf"),
    path("production/transfer-slip/", views.transfer_slip, name="transfer_slip"),
    path("jobcard/<int:pk>/header-pdf/", views.jobcard_header_pdf, name="jobcard_header_pdf"),
    path("production/urgent-only/", views.urgent_only_view, name="urgent_only"),

    path("chat/", chatbot_views.chat_page, name="chat"),
    path("api/chat/", chatbot_views.chat_api, name="chat_api"),
]
