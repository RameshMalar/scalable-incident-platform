"""One client for centralized access and Telegram APIs in Subscription Service."""
import json
import os
from urllib.parse import urlparse, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import URLError
from uuid import UUID

from access_roles import InvalidAccess, parse_grant


class AccessAPIUnavailable(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


PENDING_DESTINATIONS = '/api/v1/admin/telegram/pending-destinations'


def request_access(method, path, token, payload=None, *, opener=None, cursor=None, tenant_id=None):
    origin = os.environ.get('SUBSCRIPTION_API_ORIGIN','')
    parsed = urlparse(origin)
    root = '/api/v1/admin/access/users'
    if cursor is not None:
        try:UUID(cursor)
        except (ValueError,TypeError,AttributeError):raise AccessAPIUnavailable('Invalid page cursor') from None
        if method!='GET' or path!=root:raise AccessAPIUnavailable('Invalid page request')
    # Destination review is read-only: GET with one tenant, nothing else.
    if (path==PENDING_DESTINATIONS) != (tenant_id is not None):
        raise AccessAPIUnavailable('Invalid destination review request')
    if tenant_id is not None and (method!='GET' or payload is not None or not isinstance(tenant_id,str)
                                  or not tenant_id or not tenant_id.replace('-','').replace('_','').isalnum()):
        raise AccessAPIUnavailable('Invalid destination review request')
    if path not in ('/api/v1/access/me', root, PENDING_DESTINATIONS):
        if not path.startswith(root+'/'):
            raise AccessAPIUnavailable('Unsupported access endpoint')
        try:UUID(path[len(root)+1:])
        except ValueError:raise AccessAPIUnavailable('Invalid subject') from None
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.path not in ('','/')
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise AccessAPIUnavailable('Centralized access is not configured')
    if not isinstance(token,str) or not token or '\r' in token or '\n' in token:
        raise AccessAPIUnavailable('A delegated API token is required')
    body = None if payload is None else json.dumps(payload).encode()
    query={key:value for key,value in (('cursor',cursor),('tenant_id',tenant_id)) if value is not None}
    suffix='?'+urlencode(query) if query else ''
    request = Request(origin.rstrip('/')+path+suffix,data=body,method=method,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        with (opener or build_opener(NoRedirect())).open(request,timeout=10) as response:
            value=json.loads(response.read(262144))
            if response.status not in (200,201) or not isinstance(value,dict):
                raise AccessAPIUnavailable('Centralized access unavailable')
            return value
    except (ValueError,URLError,OSError):
        # No response bodies, tokens, private destinations or transport details.
        raise AccessAPIUnavailable('Centralized access unavailable') from None


def current_grant(identity, token, approved_tenants):
    value=request_access('GET','/api/v1/access/me',token)
    if value.get('contract') != 'central-access-v1':
        raise AccessAPIUnavailable('Central access contract not verified')
    return parse_grant(value,identity.directory_id,approved_tenants,expected_user=identity.user_id)
