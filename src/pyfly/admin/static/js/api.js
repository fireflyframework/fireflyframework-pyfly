/** HTTP client shared by dashboard views. */
export class APIError extends Error {
    constructor(status, payload) {
        super(payload.error || payload.detail || `API error: ${status}`);
        this.status = status;
        this.errors = payload.errors || [];
    }
}

export class AdminAPI {
    constructor(basePath = new URL('api', document.baseURI).pathname) {
        this.basePath = basePath.replace(/\/$/, '');
    }

    async request(method, endpoint, body, options = {}) {
        const headers = { ...options.headers };
        if (body !== undefined) headers['Content-Type'] = 'application/json';
        if (!['GET', 'HEAD'].includes(method)) {
            const cookie = document.cookie.split(';').map(v => v.trim()).find(v => v.startsWith('XSRF-TOKEN='));
            if (cookie) headers['X-XSRF-TOKEN'] = decodeURIComponent(cookie.slice('XSRF-TOKEN='.length));
        }
        const response = await fetch(`${this.basePath}${endpoint}`, {
            ...options, method, headers, credentials: 'same-origin',
            body: body === undefined ? undefined : JSON.stringify(body),
        });
        if (!response.ok) {
            let payload;
            try { payload = await response.json(); } catch (_) { payload = {}; }
            throw new APIError(response.status, payload);
        }
        return response.status === 204 ? null : response.json();
    }

    get(endpoint, options) { return this.request('GET', endpoint, undefined, options); }
    post(endpoint, body = {}, options) { return this.request('POST', endpoint, body, options); }
    patch(endpoint, body, options) { return this.request('PATCH', endpoint, body, options); }
    delete(endpoint, options) { return this.request('DELETE', endpoint, undefined, options); }
}

export const api = new AdminAPI();
