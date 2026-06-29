import os


class NormalizeUnderscoreHostMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = request.META.get("HTTP_HOST", "")
        domain, separator, port = host.partition(":")
        if "_" in domain:
            replacement = self._replacement_host()
            if replacement:
                request.META["HTTP_HOST"] = f"{replacement}{separator}{port}" if port else replacement
        return self.get_response(request)

    def _replacement_host(self):
        configured = os.getenv("CRM_UNDERSCORE_HOST_REPLACEMENT", "").strip()
        if configured:
            return configured
        for candidate in os.getenv("DJANGO_ALLOWED_HOSTS", "").split(","):
            candidate = candidate.strip()
            if candidate and "_" not in candidate and candidate not in {"localhost", "127.0.0.1"}:
                return candidate
        return "localhost"
