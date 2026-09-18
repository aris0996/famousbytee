from werkzeug.security import check_password_hash, generate_password_hash
from html import escape as html_escape
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit


_HASH_PREFIXES = ('scrypt:', 'pbkdf2:')


def is_password_hash(value):
    return bool(value) and str(value).startswith(_HASH_PREFIXES)


def hash_password(value):
    return generate_password_hash(str(value), method='scrypt')


def password_validation_error(value):
    password = str(value or '')
    if len(password) < 8:
        return 'Password minimal 8 karakter.'
    if len(password) > 128:
        return 'Password maksimal 128 karakter.'
    if not re.search(r'[A-Za-z]', password) or not re.search(r'\d', password):
        return 'Password harus mengandung huruf dan angka.'
    return None


def verify_password(stored_value, candidate):
    stored = str(stored_value or '')
    if not stored:
        return False, False
    if is_password_hash(stored):
        return check_password_hash(stored, candidate), False
    return hmac_safe_equal(stored, candidate), True


def hmac_safe_equal(left, right):
    import hmac
    return hmac.compare_digest(str(left), str(right))


_SAFE_RICH_TAGS = {
    'p', 'br', 'strong', 'b', 'em', 'i', 'u', 's', 'ul', 'ol', 'li',
    'h2', 'h3', 'h4', 'blockquote', 'pre', 'code', 'a', 'img'
}
_SAFE_RICH_ATTRS = {
    'a': {'href', 'title'},
    'img': {'src', 'alt', 'title'},
}
_DROP_RICH_TAGS = {'script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'form', 'template'}


def _safe_content_url(value):
    value = str(value or '').strip()
    if not value:
        return None
    parsed = urlsplit(value)
    if value.startswith('/') and not value.startswith('//'):
        return value
    if parsed.scheme.lower() in {'http', 'https'} and parsed.netloc:
        return value
    return None


class _SafeRichTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.drop_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.drop_depth:
            self.drop_depth += 1
            return
        if tag in _DROP_RICH_TAGS:
            self.drop_depth = 1
            return
        if tag not in _SAFE_RICH_TAGS:
            return
        safe_attrs = []
        for name, value in attrs:
            name = name.lower()
            if name not in _SAFE_RICH_ATTRS.get(tag, set()):
                continue
            if name in {'href', 'src'}:
                value = _safe_content_url(value)
                if not value:
                    continue
            safe_attrs.append(f' {name}="{html_escape(str(value or ""), quote=True)}"')
        self.output.append(f'<{tag}{"".join(safe_attrs)}>')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.drop_depth:
            self.drop_depth -= 1
            return
        if tag in _SAFE_RICH_TAGS and tag != 'br':
            self.output.append(f'</{tag}>')

    def handle_data(self, data):
        if not self.drop_depth:
            self.output.append(html_escape(data, quote=False))


def sanitize_rich_text(value):
    """Keep a small formatting allowlist and remove executable HTML."""
    parser = _SafeRichTextParser()
    parser.feed(str(value or ''))
    parser.close()
    return ''.join(parser.output)


def safe_external_url(value, allowed_hosts=None, allow_local=True):
    """Return a safe URL or an inert '#' value for public templates."""
    value = str(value or '').strip()
    if not value or value == '#':
        return '#'
    if allow_local and value.startswith('/') and not value.startswith('//'):
        return value
    parsed = urlsplit(value)
    if parsed.scheme.lower() != 'https' or not parsed.netloc or parsed.username or parsed.password:
        return '#'
    hostname = (parsed.hostname or '').lower().rstrip('.')
    if allowed_hosts:
        allowed = {str(host).lower().rstrip('.') for host in allowed_hosts}
        if hostname not in allowed and not any(hostname.endswith('.' + host) for host in allowed):
            return '#'
    return value
