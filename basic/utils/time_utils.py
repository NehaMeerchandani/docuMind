from zoneinfo import ZoneInfo

from django.utils import timezone


class TimeUtils:
    @staticmethod
    def now():
        return timezone.now()

    @staticmethod
    def to_utc(dt):
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, ZoneInfo('UTC'))
        return dt.astimezone(ZoneInfo('UTC'))

    @staticmethod
    def to_timezone(dt, tz_name):
        return dt.astimezone(ZoneInfo(tz_name))
