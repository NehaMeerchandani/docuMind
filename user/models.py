from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from base.models import BaseModel
from basic.constants.roles import CompanyRoles
from basic.utils.crypto import TokenGenerator


class CustomUser(AbstractUser):
    email = models.EmailField(unique=True)
    companies = models.ManyToManyField(
        'company.Company',
        through='CompanyMembership',
        through_fields=('user', 'company'),
        related_name='members',
    )

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.email


class CompanyMembership(BaseModel):
    company = models.ForeignKey(
        'company.Company',
        on_delete=models.CASCADE,
        related_name='memberships',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='company_memberships',
    )
    role = models.CharField(max_length=20, choices=CompanyRoles.CHOICES, default=CompanyRoles.MEMBER)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f'{self.user} @ {self.company} ({self.role})'


class RefreshToken(BaseModel):
    LIFETIME_DAYS = 7

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='refresh_tokens',
    )
    token = models.CharField(max_length=255, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    is_revoked = models.BooleanField(default=False)

    class Meta:
        ordering = ['-id']

    @classmethod
    def issue_for_user(cls, user):
        token_value = TokenGenerator.generate_token()
        cls.objects.create(
            user=user,
            token=token_value,
            expires_at=timezone.now() + timezone.timedelta(days=cls.LIFETIME_DAYS),
        )
        return token_value

    def __str__(self):
        return f'{self.user} refresh token (revoked={self.is_revoked})'
