from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from base.views import CompanyScopedMixin, EnvelopeResponseMixin
from basic.utils.response import APIResponse
from document.models import Document
from document.permissions import IsUploaderOrCompanyAdmin
from document.serializers import DocumentSerializer, DocumentUploadSerializer
from document.services.document_service import DocumentService
from user.models import CompanyMembership


class DocumentListView(EnvelopeResponseMixin, CompanyScopedMixin, generics.ListAPIView):
    serializer_class = DocumentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        company = self.get_active_company(self.request)
        return Document.objects.filter(company=company)


class DocumentUploadView(EnvelopeResponseMixin, CompanyScopedMixin, APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company = self.get_active_company(request)

        serializer = DocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        document = serializer.save(company=company, created_by=request.user)

        return APIResponse.success(
            data=DocumentSerializer(document).data,
            message='Document uploaded. Call the process endpoint to extract and embed it.',
            status_code=status.HTTP_201_CREATED,
        )


class DocumentProcessView(EnvelopeResponseMixin, APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            document = Document.objects.get(pk=pk)
        except Document.DoesNotExist:
            return APIResponse.error('Document not found.', status_code=status.HTTP_404_NOT_FOUND)

        if not CompanyMembership.objects.filter(user=request.user, company=document.company).exists():
            return APIResponse.error('You are not a member of this company.', status_code=status.HTTP_403_FORBIDDEN)

        try:
            DocumentService.process(document)
        except Exception as exc:
            return APIResponse.error(str(exc), status_code=status.HTTP_400_BAD_REQUEST)

        return APIResponse.success(
            data=DocumentSerializer(document).data,
            message='Document processed successfully.',
        )


class DocumentDeleteView(EnvelopeResponseMixin, APIView):
    permission_classes = [IsAuthenticated, IsUploaderOrCompanyAdmin]

    def delete(self, request, pk):
        try:
            document = Document.objects.get(pk=pk)
        except Document.DoesNotExist:
            return APIResponse.error('Document not found.', status_code=status.HTTP_404_NOT_FOUND)

        self.check_object_permissions(request, document)
        document.delete()

        return APIResponse.success(message='Document deleted.')
