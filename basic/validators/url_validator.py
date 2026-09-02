import ipaddress
import socket
from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible


@deconstructible
class SSRFSafeURLValidator:
    ALLOWED_SCHEMES = {'http', 'https'}

    def __call__(self, value):
        parsed = urlparse(value)

        if parsed.scheme not in self.ALLOWED_SCHEMES:
            raise ValidationError('URL must start with http:// or https://.')

        if not parsed.hostname:
            raise ValidationError('URL must include a valid hostname.')

        try:
            resolved = socket.getaddrinfo(parsed.hostname, None)
        except socket.gaierror:
            raise ValidationError('URL hostname could not be resolved.')

        for _, _, _, _, sockaddr in resolved:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise ValidationError('URL resolves to a private or internal address, which is not allowed.')
