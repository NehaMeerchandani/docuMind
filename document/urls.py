from django.urls import path

from document.views import DocumentDeleteView, DocumentListView, DocumentProcessView, DocumentUploadView

urlpatterns = [
    path('', DocumentListView.as_view(), name='document-list'),
    path('upload/', DocumentUploadView.as_view(), name='document-upload'),
    path('<int:pk>/process/', DocumentProcessView.as_view(), name='document-process'),
    path('<int:pk>/', DocumentDeleteView.as_view(), name='document-delete'),
]
