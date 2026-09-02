import json

from django.http import JsonResponse, StreamingHttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework import generics
from rest_framework.exceptions import AuthenticationFailed, NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from base.views import CompanyScopedMixin, EnvelopeResponseMixin
from basic.utils.response import APIResponse
from chat.models import Conversation
from chat.serializers import ConversationSerializer, MessageSerializer
from chat.services.chat_service import ChatService
from company.models import Company
from user.authentication import JWTService
from user.models import CompanyMembership, CustomUser


class ConversationListView(EnvelopeResponseMixin, CompanyScopedMixin, generics.ListAPIView):
    serializer_class = ConversationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        company = self.get_active_company(self.request)
        return Conversation.objects.filter(company=company, user=self.request.user)


class ConversationMessagesView(EnvelopeResponseMixin, generics.ListAPIView):
    serializer_class = MessageSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        conversation = self._get_owned_conversation()
        return conversation.messages.all()

    def _get_owned_conversation(self):
        try:
            conversation = Conversation.objects.get(session_id=self.kwargs['session_id'])
        except Conversation.DoesNotExist:
            raise NotFound('Conversation not found.')

        if conversation.user_id != self.request.user.id:
            raise NotFound('Conversation not found.')

        return conversation


class ConversationDeleteView(EnvelopeResponseMixin, APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, session_id):
        try:
            conversation = Conversation.objects.get(session_id=session_id)
        except Conversation.DoesNotExist:
            return APIResponse.error('Conversation not found.', status_code=404)

        if conversation.user_id != request.user.id:
            return APIResponse.error('Conversation not found.', status_code=404)

        conversation.delete()
        return APIResponse.success(message='Conversation deleted.')


@method_decorator(csrf_exempt, name='dispatch')
class ChatStreamView(View):
    async def post(self, request):
        try:
            user = await self._authenticate(request)
            body = json.loads(request.body or b'{}')
            company = await self._get_membership_checked_company(user, body.get('company_id'))
            question = body.get('question')
            if not question:
                return JsonResponse({'success': False, 'error': 'question is required.', 'data': None}, status=400)

            conversation = await self._get_or_create_conversation(body.get('session_id'), user, company)
        except _ChatRequestError as exc:
            return JsonResponse({'success': False, 'error': exc.message, 'data': None}, status=exc.status_code)

        response = StreamingHttpResponse(
            ChatService.stream_reply(conversation, company, question),
            content_type='text/event-stream',
        )
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response

    async def _authenticate(self, request):
        auth_header = request.headers.get('Authorization', '')
        parts = auth_header.split()
        if len(parts) != 2 or parts[0] != 'Bearer':
            raise _ChatRequestError('Authorization header must be in the form: Bearer <token>.', 401)

        try:
            payload = JWTService.decode_access_token(parts[1])
        except AuthenticationFailed as exc:
            raise _ChatRequestError(str(exc), 401)

        try:
            return await CustomUser.objects.aget(id=payload['user_id'], is_active=True)
        except CustomUser.DoesNotExist:
            raise _ChatRequestError('User not found or inactive.', 401)

    async def _get_membership_checked_company(self, user, company_id):
        if not company_id:
            raise _ChatRequestError('company_id is required.', 400)

        try:
            company = await Company.objects.aget(id=company_id, is_active=True)
        except Company.DoesNotExist:
            raise _ChatRequestError('Company not found.', 404)

        is_member = await CompanyMembership.objects.filter(user=user, company=company).aexists()
        if not is_member:
            raise _ChatRequestError('You are not a member of this company.', 403)

        return company

    async def _get_or_create_conversation(self, session_id, user, company):
        if not session_id:
            return await Conversation.objects.acreate(company=company, user=user, created_by=user)

        try:
            conversation = await Conversation.objects.aget(session_id=session_id)
        except Conversation.DoesNotExist:
            raise _ChatRequestError('Conversation not found.', 404)

        if conversation.user_id != user.id:
            raise _ChatRequestError('Conversation not found.', 404)

        return conversation


class _ChatRequestError(Exception):
    def __init__(self, message, status_code):
        self.message = message
        self.status_code = status_code
        super().__init__(message)
