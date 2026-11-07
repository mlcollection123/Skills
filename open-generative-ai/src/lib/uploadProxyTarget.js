const DEFAULT_S3_REGION_PATTERN = /^[a-z0-9-]+$/;

function normalizeHostname(hostname) {
    return hostname.toLowerCase().replace(/\.$/, '');
}

function parseAllowedHosts(env) {
    return (env.UPLOAD_PROXY_ALLOWED_HOSTS || '')
        .split(',')
        .map((host) => normalizeHostname(host.trim()))
        .filter(Boolean);
}

function parseIpV4(hostname) {
    const parts = hostname.split('.');
    if (parts.length !== 4 || parts.some((part) => !/^\d+$/.test(part))) {
        return null;
    }

    const octets = parts.map((part) => Number(part));
    if (octets.some((octet) => octet < 0 || octet > 255)) {
        return null;
    }

    return octets;
}

function isIpLiteral(hostname) {
    return Boolean(parseIpV4(hostname)) || hostname.includes(':');
}

function isBlockedIpV4(hostname) {
    const octets = parseIpV4(hostname);
    if (!octets) {
        return false;
    }

    const [first, second] = octets;
    return (
        first === 0 ||
        first === 10 ||
        first === 127 ||
        (first === 169 && second === 254) ||
        (first === 172 && second >= 16 && second <= 31) ||
        (first === 192 && second === 168)
    );
}

function isBlockedHost(hostname) {
    const normalized = normalizeHostname(hostname).replace(/^\[|\]$/g, '');

    return (
        normalized === 'localhost' ||
        normalized === '::1' ||
        isIpLiteral(normalized) ||
        isBlockedIpV4(normalized)
    );
}

function isAllowedS3Host(hostname) {
    // Reject empty labels (leading dot, trailing dot, or consecutive dots).
    if (hostname.split('.').some((label) => label === '')) {
        return false;
    }

    if (hostname === 's3.amazonaws.com') {
        return true;
    }

    if (hostname.endsWith('.s3.amazonaws.com')) {
        return hostname.length > '.s3.amazonaws.com'.length;
    }

    const labels = hostname.split('.');
    if (labels.length === 4 && labels[0] === 's3' && labels[2] === 'amazonaws' && labels[3] === 'com') {
        return DEFAULT_S3_REGION_PATTERN.test(labels[1]);
    }

    if (labels.length >= 5 && labels[labels.length - 2] === 'amazonaws' && labels[labels.length - 1] === 'com') {
        const s3LabelIndex = labels.findIndex((label) => label === 's3');
        return (
            s3LabelIndex > 0 &&
            labels.length - s3LabelIndex === 4 &&
            DEFAULT_S3_REGION_PATTERN.test(labels[s3LabelIndex + 1])
        );
    }

    return false;
}

export function getApiKeyFromRequest(request) {
    if (!request || !request.headers) return null;
    const authHeader = request.headers.get('Authorization');
    if (authHeader && authHeader.startsWith('Bearer ')) {
        const token = authHeader.substring(7).trim();
        if (token) return token;
    }
    const headerKey = request.headers.get('x-api-key');
    if (headerKey && headerKey.trim()) {
        return headerKey.trim();
    }
    return null;
}

const BLOCKED_EXTENSIONS = new Set([
    'html', 'htm', 'xhtml', 'svg', 'php', 'phtml', 'php3', 'php4', 'php5', 'phps',
    'exe', 'bat', 'cmd', 'sh', 'bash', 'js', 'cgi', 'pl', 'py', 'jar', 'vbs', 'scr', 'msi'
]);

const BLOCKED_MIME_TYPES = new Set([
    'text/html',
    'image/svg+xml',
    'application/xhtml+xml',
    'application/x-httpd-php',
    'application/x-msdownload',
    'application/x-executable',
    'application/x-sh',
    'application/x-shellscript',
    'application/javascript',
    'text/javascript'
]);

export function isBlockedFileType(filename = '', contentType = '') {
    if (contentType) {
        const normalizedMime = contentType.toLowerCase().split(';')[0].trim();
        if (BLOCKED_MIME_TYPES.has(normalizedMime)) {
            return true;
        }
    }
    if (filename) {
        const parts = filename.toLowerCase().split('.');
        if (parts.length > 1) {
            const ext = parts[parts.length - 1].trim();
            if (BLOCKED_EXTENSIONS.has(ext)) {
                return true;
            }
        }
    }
    return false;
}

export function validateUploadProxyTarget(rawTarget, { env = process.env } = {}) {
    if (typeof rawTarget !== 'string' || rawTarget.trim() === '') {
        return { ok: false, reason: 'missing_target' };
    }

    let url;
    try {
        url = new URL(rawTarget);
    } catch {
        return { ok: false, reason: 'invalid_url' };
    }

    if (url.protocol !== 'https:') {
        return { ok: false, reason: 'unsafe_protocol' };
    }

    const hostname = normalizeHostname(url.hostname);
    if (isBlockedHost(hostname)) {
        return { ok: false, reason: 'host_not_allowed' };
    }

    const allowedHosts = parseAllowedHosts(env);
    if (!isAllowedS3Host(hostname) && !allowedHosts.includes(hostname)) {
        return { ok: false, reason: 'host_not_allowed' };
    }

    return { ok: true, url: url.toString() };
}

