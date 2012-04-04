"""
    Implements the user agent used to communicate with the Slumber
    servers.
"""
from django.conf import settings
from django.core.cache import cache
from django.test.client import Client as FakeClient, encode_multipart, \
    BOUNDARY
from django.utils.http import urlencode

from datetime import datetime
from fost_authn.signature import fost_hmac_request_signature
from httplib2 import Http
import logging
from simplejson import loads
from urllib import urlencode
from urlparse import parse_qs, urlparse

from slumber._caches import PER_THREAD
from slumber.server import get_slumber_local_url_prefix


_fake = FakeClient()


def _parse_qs(url):
    """Split the query string off (this is needed to support Django 1.0's
    fake HTTP client.
    """
    if url.find('?') >= 0:
        path, query_string = url.split('?')
        return path, parse_qs(query_string)
    else:
        return url, {}


def _use_fake(url):
    """Return the local URL fragment if the request should use the Fake
    HTTP client as it is local, otherwise return None
    """
    slumber_local = get_slumber_local_url_prefix()
    if url.startswith(slumber_local):
        logging.debug("Using local fake HTTP due to %s starting with %s",
            url, slumber_local)
        return url[len(slumber_local) - 1:]
    elif url.startswith('/'):
        logging.debug("Using local fake HTTP due to %s starting with /",
            url)
        return url
    logging.debug("Using real HTTP for %s", url)


def _calculate_signature(authn_name, method, url, body,
        username, for_fake_client):
    """Do the signed request calculation.
    """
    # We need all arguments and all locals
    # pylint: disable=R0913
    # pylint: disable=R0914
    to_sign = {}
    if username:
        to_sign['X-FOST-User'] = username
    if for_fake_client:
        if method in ['POST', 'PUT']:
            logging.info("Encoding POST/PUT data %s", body or {})
            data = encode_multipart(BOUNDARY, body or {})
        else:
            logging.info("Encoding query string %s", body or {})
            data = body if isinstance(body, basestring) else \
                urlencode(body or {}, doseq=True)
    else:
        data = body or ''
    now = datetime.utcnow().isoformat() + 'Z'
    _, signature = fost_hmac_request_signature(
        settings.SECRET_KEY, method, url, now, to_sign, data)
    headers = {}
    headers['Authorization'] = 'FOST %s:%s' % (authn_name, signature)
    headers['X-FOST-Timestamp'] = now
    headers['X-FOST-Headers'] = ' '.join(['X-FOST-Headers'] + to_sign.keys())
    for key, value in to_sign.items():
        headers[key] = value
    logging.debug("_calculate_signature %s adding headers: %s", method, headers)
    if for_fake_client:
        return dict([('HTTP_' + k.upper().replace('-', '_'), v)
            for k, v in headers.items()])
    else:
        return headers


def _sign_request(method, url, body, for_fake_client):
    """Calculate the request headers that need to be added so that the
    request is properly signed and the Slumber server will consider
    the current user to be authenticated.
    """
    # import here avoids circular import
    from slumber.connector import get_slumber_authn_name
    authn_name = get_slumber_authn_name()
    if authn_name:
        request, username = getattr(PER_THREAD, 'request', None), None
        if request and request.user.is_authenticated():
            username = request.user.username
        return _calculate_signature(
            authn_name, method, url, body, username, for_fake_client)
    else:
        return {}


def get(url, ttl = 0):
    """Perform a GET request against a Slumber server.
    """
    # Pylint gets confused by the fake HTTP client
    # pylint: disable=E1103
    url_fragment = _use_fake(url)
    if url_fragment:
        file_spec, query = _parse_qs(url_fragment)
        headers = _sign_request('GET', file_spec, query, True)
        response = _fake.get(file_spec, query,
            HTTP_HOST='localhost:8000', **headers)
        if response.status_code in [301, 302]:
            return get(response['location'])
        assert response.status_code == 200, (
            url_fragment, response.status_code)
        content = response.content
    else:
        cache_key = 'slumber.connector.ua.get.%s' % url
        cached = cache.get(cache_key)
        if not cached:
            logging.debug("Cache miss for url %s with cache key %s",
                url, cache_key)
            _, _, path, _, query, _ = urlparse(url)
            to_sign = path + ('' if not query else '?' + query)
            for _ in range(0, 3):
                headers = _sign_request('GET', to_sign, '', False)
                response, content = Http().request(
                    url, headers=headers)
                if response.status == 200:
                    break
            assert response.status == 200, (url, response.status)
            if ttl:
                cache.set(cache_key, (response, content), ttl)
        else:
            logging.debug("Fetched %s from cache key %s", url, cache_key)
            response, content = cached
            response.from_cache = True
    return response, loads(content)


def post(url, data):
    """Perform a POST request against a Slumber server.
    """
    # Pylint gets confused by the fake HTTP client
    # pylint: disable=E1103
    url_fragment = _use_fake(url)
    if url_fragment:
        response = _fake.post(url_fragment, data,
            HTTP_HOST='localhost:8000',
            **_sign_request('POST', url_fragment, data, True))
        assert response.status_code == 200, \
            (url_fragment, response, response.content)
        content = response.content
    else:
        body = urlencode(data)
        response, content = Http().request(url, "POST", body=body,
            headers = _sign_request('POST', url, data, False))
        assert response.status == 200, content
    return response, loads(content)

