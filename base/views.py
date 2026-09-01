class EnvelopeResponseMixin:
    def finalize_response(self, request, response, *args, **kwargs):
        if not response.exception and not (isinstance(response.data, dict) and 'success' in response.data):
            response.data = {
                'success': True,
                'message': 'Success',
                'data': response.data,
            }
        return super().finalize_response(request, response, *args, **kwargs)
