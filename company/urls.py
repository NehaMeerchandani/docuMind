from django.urls import path

from company.views import CompanyListView

urlpatterns = [
    path('', CompanyListView.as_view(), name='company-list'),
]
