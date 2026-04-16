from django.urls import path

from .views import calendar_view, dashboard_view, page_view

app_name = "arabela_admin"

urlpatterns = [
    path("", dashboard_view, name="dashboard"),
    path("calendar/", calendar_view, name="calendar"),
    path("<str:page>.html", page_view, name="page"),
]
