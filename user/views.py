from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from base.views import EnvelopeResponseMixin
from basic.constants.errors import ErrorMessages
from basic.utils.response import APIResponse
from user.authentication import JWTService
from user.models import RefreshToken
from user.serializers import LoginSerializer, RefreshSerializer, RegisterSerializer, UserSerializer


class RegisterView(EnvelopeResponseMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return APIResponse.success(
            data=UserSerializer(user).data,
            message='Registration successful.',
            status_code=status.HTTP_201_CREATED,
        )


class LoginView(EnvelopeResponseMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']

        access_token = JWTService.generate_access_token(user)
        refresh_token = RefreshToken.issue_for_user(user)

        return APIResponse.success(
            data={
                'access_token': access_token,
                'refresh_token': refresh_token,
                'user': UserSerializer(user).data,
            },
            message='Login successful.',
        )


class RefreshView(EnvelopeResponseMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token_value = serializer.validated_data['refresh_token']

        try:
            old_token = RefreshToken.objects.get(
                token=token_value,
                is_revoked=False,
                expires_at__gt=timezone.now(),
            )
        except RefreshToken.DoesNotExist:
            return APIResponse.error(ErrorMessages.TOKEN_INVALID, status_code=status.HTTP_401_UNAUTHORIZED)

        old_token.is_revoked = True
        old_token.save(update_fields=['is_revoked'])

        new_access_token = JWTService.generate_access_token(old_token.user)
        new_refresh_token = RefreshToken.issue_for_user(old_token.user)

        return APIResponse.success(
            data={
                'access_token': new_access_token,
                'refresh_token': new_refresh_token,
            },
            message='Token refreshed.',
        )


class LogoutView(EnvelopeResponseMixin, APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token_value = serializer.validated_data['refresh_token']

        RefreshToken.objects.filter(token=token_value, user=request.user).update(is_revoked=True)

        return APIResponse.success(message='Logged out successfully.')


class MeView(EnvelopeResponseMixin, APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return APIResponse.success(data=UserSerializer(request.user).data)
