from rest_framework import generics
from rest_framework.permissions import AllowAny

from base.views import EnvelopeResponseMixin
from company.models import Company
from company.serializers import CompanySerializer


class CompanyListView(EnvelopeResponseMixin, generics.ListAPIView):
    queryset = Company.objects.filter(is_active=True)
    serializer_class = CompanySerializer
    permission_classes = [AllowAny]
    authentication_classes = []
