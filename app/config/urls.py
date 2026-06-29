from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.views.static import serve as static_serve
from django.urls import path, re_path

from crm import views


urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="login"), name="logout"),
    path("", views.dashboard, name="dashboard"),
    path("customers/", views.customer_list, name="customer_list"),
    path("customers/export/", views.customer_export, name="customer_export"),
    path("customers/import/", views.customer_import, name="customer_import"),
    path("customers/import/confirm/", views.customer_import_confirm, name="customer_import_confirm"),
    path("customers/bulk/", views.customer_bulk_action, name="customer_bulk_action"),
    path("customers/new/", views.customer_create, name="customer_create"),
    path("customers/<int:pk>/", views.customer_detail, name="customer_detail"),
    path("customers/<int:pk>/edit/", views.customer_edit, name="customer_edit"),
    path("customers/<int:pk>/inline-update/", views.customer_inline_update, name="customer_inline_update"),
    path("customers/<int:pk>/claim/", views.customer_claim, name="customer_claim"),
    path("customers/<int:pk>/transfer/", views.customer_transfer, name="customer_transfer"),
    path("customers/<int:pk>/merge/", views.customer_merge, name="customer_merge"),
    path("customers/<int:pk>/mark-invalid/", views.customer_mark_invalid, name="customer_mark_invalid"),
    path("customers/<int:pk>/recycle-restore/", views.customer_recycle_restore, name="customer_recycle_restore"),
    path("customers/<int:pk>/delete/", views.customer_delete, name="customer_delete"),
    path("customers/<int:pk>/restore/", views.customer_restore, name="customer_restore"),
    path("customers/history/", views.customer_history_action, name="customer_history_action"),
    path("contact-logs/", views.contact_log_list, name="contact_log_list"),
    path("contact-logs/new/", views.contact_log_create, name="contact_log_create"),
    path("contact-logs/<int:pk>/edit/", views.contact_log_edit, name="contact_log_edit"),
    path("contact-logs/<int:pk>/inline-update/", views.contact_log_inline_update, name="contact_log_inline_update"),
    path("contact-logs/<int:pk>/delete/", views.contact_log_delete, name="contact_log_delete"),
    path("contracts/", views.contract_list, name="contract_list"),
    path("contracts/new/", views.contract_create, name="contract_create"),
    path("contracts/<int:pk>/edit/", views.contract_edit, name="contract_edit"),
    path("contracts/<int:pk>/inline-update/", views.contract_inline_update, name="contract_inline_update"),
    path("contracts/<int:pk>/delete/", views.contract_delete, name="contract_delete"),
    path("contracts/<int:pk>/restore/", views.contract_restore, name="contract_restore"),
    path("deleted/", views.deleted_list, name="deleted_list"),
    path("leads/", views.lead_list, name="lead_list"),
    path("leads/new/", views.lead_create, name="lead_create"),
    path("leads/<int:pk>/edit/", views.lead_edit, name="lead_edit"),
    path("leads/<int:pk>/convert/", views.lead_convert, name="lead_convert"),
    path("leads/<int:pk>/mark-invalid/", views.lead_mark_invalid, name="lead_mark_invalid"),
    path("opportunities/", views.opportunity_list, name="opportunity_list"),
    path("opportunities/new/", views.opportunity_create, name="opportunity_create"),
    path("opportunities/<int:pk>/edit/", views.opportunity_edit, name="opportunity_edit"),
    path("quotes/", views.quote_list, name="quote_list"),
    path("quotes/new/", views.quote_create, name="quote_create"),
    path("quotes/<int:pk>/edit/", views.quote_edit, name="quote_edit"),
    path("payments/new/", views.payment_create, name="payment_create"),
    path("visits/", views.visit_list, name="visit_list"),
    path("visits/new/", views.visit_create, name="visit_create"),
    path("reminders/", views.reminder_list, name="reminder_list"),
    path("reminders/new/", views.task_create, name="task_create"),
    path("reports/", views.report_analysis, name="report_analysis"),
    path("api/intake/contact-log/", views.intake_contact_log_api, name="intake_contact_log_api"),
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
urlpatterns += [re_path(r"^static/(?P<path>.*)$", static_serve, {"document_root": settings.STATIC_ROOT})]
