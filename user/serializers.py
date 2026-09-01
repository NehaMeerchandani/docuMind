from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError as DjangoValidationError
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from basic.constants.errors import ErrorMessages
from basic.constants.roles import CompanyRoles
from company.models import Company
from user.models import CompanyMembership, CustomUser


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)
    password_confirm = serializers.CharField(write_only=True)
    company_ids = serializers.ListField(
        child=serializers.IntegerField(),
        allow_empty=False,
        write_only=True,
    )

    class Meta:
        model = CustomUser
        fields = ['id', 'username', 'email', 'password', 'password_confirm', 'company_ids']
        read_only_fields = ['id']

    def validate(self, attrs):
        if attrs['password'] != attrs['password_confirm']:
            raise serializers.ValidationError({'password_confirm': 'Passwords do not match.'})

        try:
            validate_password(attrs['password'])
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)})

        company_ids = set(attrs['company_ids'])
        companies = Company.objects.filter(id__in=company_ids, is_active=True)
        if companies.count() != len(company_ids):
            raise serializers.ValidationError({'company_ids': 'One or more companies could not be found.'})

        attrs['companies'] = companies
        return attrs

    def create(self, validated_data):
        companies = validated_data.pop('companies')
        validated_data.pop('company_ids')
        validated_data.pop('password_confirm')
        password = validated_data.pop('password')

        user = CustomUser(**validated_data)
        user.set_password(password)
        user.save()

        for company in companies:
            CompanyMembership.objects.create(user=user, company=company, role=CompanyRoles.MEMBER)

        return user


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get('request'),
            username=attrs['email'],
            password=attrs['password'],
        )
        if user is None:
            raise serializers.ValidationError(ErrorMessages.INVALID_CREDENTIALS)

        attrs['user'] = user
        return attrs


class RefreshSerializer(serializers.Serializer):
    refresh_token = serializers.CharField()


class CompanyMembershipSerializer(serializers.ModelSerializer):
    company_id = serializers.IntegerField(source='company.id')
    company_name = serializers.CharField(source='company.name')

    class Meta:
        model = CompanyMembership
        fields = ['company_id', 'company_name', 'role']


class UserSerializer(serializers.ModelSerializer):
    companies = CompanyMembershipSerializer(source='company_memberships', many=True, read_only=True)

    class Meta:
        model = CustomUser
        fields = ['id', 'username', 'email', 'companies']
