from rest_framework import status
from rest_framework.response import Response


class APIResponse:
    @staticmethod
    def success(data=None, message='Success', status_code=status.HTTP_200_OK):
        return Response(
            {
                'success': True,
                'message': message,
                'data': data,
            },
            status=status_code,
        )

    @staticmethod
    def error(error, status_code=status.HTTP_400_BAD_REQUEST, data=None):
        return Response(
            {
                'success': False,
                'error': error,
                'data': data,
            },
            status=status_code,
        )
