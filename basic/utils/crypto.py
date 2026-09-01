import secrets


class TokenGenerator:
    @staticmethod
    def generate_token(length=64):
        return secrets.token_urlsafe(length)
