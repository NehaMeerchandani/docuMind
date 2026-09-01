import jwt
from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from basic.constants.errors import ErrorMessages
from user.models import CustomUser


class JWTService:
    ALGORITHM = 'HS256'
    ACCESS_TOKEN_LIFETIME_MINUTES = 15

    @classmethod
    def generate_access_token(cls, user):
        now = timezone.now()
        payload = {
            'user_id': user.id,
            'token_type': 'access',
            'iat': int(now.timestamp()),
            'exp': int((now + timezone.timedelta(minutes=cls.ACCESS_TOKEN_LIFETIME_MINUTES)).timestamp()),
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=cls.ALGORITHM)

    @classmethod
    def decode_access_token(cls, token):
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[cls.ALGORITHM])
        except jwt.ExpiredSignatureError:
            raise AuthenticationFailed(ErrorMessages.TOKEN_EXPIRED)
        except jwt.InvalidTokenError:
            raise AuthenticationFailed(ErrorMessages.TOKEN_INVALID)

        if payload.get('token_type') != 'access':
            raise AuthenticationFailed(ErrorMessages.TOKEN_INVALID)

        return payload


class JWTAuthentication(BaseAuthentication):
    keyword = 'Bearer'

    def authenticate(self, request):
        auth_header = request.headers.get('Authorization')

        if not auth_header:
            return None

        parts = auth_header.split()
        if len(parts) != 2 or parts[0] != self.keyword:
            raise AuthenticationFailed('Authorization header must be in the form: Bearer <token>.')

        payload = JWTService.decode_access_token(parts[1])

        try:
            user = CustomUser.objects.get(id=payload['user_id'], is_active=True)
        except CustomUser.DoesNotExist:
            raise AuthenticationFailed(ErrorMessages.ACCOUNT_INACTIVE)

        return (user, parts[1])
