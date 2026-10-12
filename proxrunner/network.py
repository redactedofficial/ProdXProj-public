"""Credentialed HTTP transport: redirects are never followed."""
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def urlopen(request, context=None, timeout=30):
    try:
        return build_opener(HTTPSHandler(context=context), NoRedirect()).open(request, timeout=timeout)
    except HTTPError as error:
        if 300 <= error.code < 400:
            error.close()
            raise ValueError("Credentialed HTTP redirects are forbidden") from None
        raise
