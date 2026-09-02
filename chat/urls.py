from django.urls import path

from chat.views import (
    ChatStreamView,
    ConversationDeleteView,
    ConversationListView,
    ConversationMessagesView,
)

urlpatterns = [
    path('', ConversationListView.as_view(), name='conversation-list'),
    path('stream/', ChatStreamView.as_view(), name='chat-stream'),
    path('<uuid:session_id>/messages/', ConversationMessagesView.as_view(), name='conversation-messages'),
    path('<uuid:session_id>/', ConversationDeleteView.as_view(), name='conversation-delete'),
]
