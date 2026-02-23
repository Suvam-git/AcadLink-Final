from django.utils import timezone
from django.core.cache import cache

from .models import Homework


class DeleteExpiredHomeworkMiddleware:
    """
    Marks active homework as overdue immediately after their deadline passes.
    Runs once per request before view logic.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Run at most once per minute to avoid doing write queries on every request.
        cache_key = 'middleware_expired_hw_sync_last_run'
        if not cache.get(cache_key):
            Homework.objects.filter(
                status='active',
                deadline__lt=timezone.now()
            ).update(status='overdue')
            cache.set(cache_key, 1, timeout=60)
        response = self.get_response(request)
        return response
