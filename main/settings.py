"""
Django settings for main project.
"""

import os
from pathlib import Path

from django.urls import reverse_lazy
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('SECRET_KEY')

DEBUG = os.getenv('DEBUG', 'False') == 'True'

ALLOWED_HOSTS = ['localhost', '127.0.0.1', 'testserver']


INSTALLED_APPS = [
    'unfold',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'rest_framework',
    'corsheaders',

    'base',
    'basic',
    'company',
    'user',
    'document',
    'chat',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'main.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'base.context_processors.active_company_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'main.wsgi.application'


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('DB_NAME'),
        'USER': os.getenv('DB_USER'),
        'PASSWORD': os.getenv('DB_PASSWORD'),
        'HOST': os.getenv('DB_HOST'),
        'PORT': os.getenv('DB_PORT'),
    }
}


AUTH_USER_MODEL = 'user.CustomUser'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
    {'NAME': 'basic.validators.password_validator.PasswordComplexityValidator'},
]


LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'user.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'base.pagination.StandardPagination',
    'EXCEPTION_HANDLER': 'base.exceptions.custom_exception_handler',
}


PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.BCryptSHA256PasswordHasher',
    'django.contrib.auth.hashers.PBKDF2PasswordHasher',
]


CORS_ALLOW_ALL_ORIGINS = DEBUG


UNFOLD = {
    'SITE_TITLE': 'RAG Admin',
    'SITE_HEADER': 'RAG Admin',
    'SIDEBAR': {
        'show_search': True,
        'navigation': [
            {
                'title': 'Chat',
                'items': [
                    {
                        'title': 'Chat',
                        'icon': 'chat',
                        'link': reverse_lazy('admin:chat_interface'),
                    },
                    {
                        'title': 'Documents',
                        'icon': 'description',
                        'link': reverse_lazy('admin:document_document_changelist'),
                    },
                    {
                        'title': 'Chunks',
                        'icon': 'segment',
                        'link': reverse_lazy('admin:document_chunk_changelist'),
                    },
                ],
            },
            {
                'title': 'Administration',
                'items': [
                    {
                        'title': 'Companies',
                        'icon': 'business',
                        'link': reverse_lazy('admin:company_company_changelist'),
                        'permission': 'main.nav_permissions.is_full_admin',
                    },
                    {
                        'title': 'Users',
                        'icon': 'people',
                        'link': reverse_lazy('admin:user_customuser_changelist'),
                        'permission': 'main.nav_permissions.is_full_admin',
                    },
                    {
                        'title': 'Conversations',
                        'icon': 'forum',
                        'link': reverse_lazy('admin:chat_conversation_changelist'),
                        'permission': 'main.nav_permissions.is_full_admin',
                    },
                ],
            },
        ],
    },
}
