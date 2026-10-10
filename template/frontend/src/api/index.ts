// Dependency-free API client (fetch). Only react, react-dom and react-router-dom are installed.
export class ApiError extends Error {
  status: number;
  data: unknown;
  constructor(message: string, status: number, data: unknown) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

export async function apiRequest<T = any>(path: string, options: { method?: string; body?: unknown; headers?: Record<string, string> } = {}): Promise<T> {
  const url = path.startsWith('/api') ? path : '/api' + (path.startsWith('/') ? path : '/' + path);
  const init: RequestInit = { method: options.method || 'GET', credentials: 'same-origin', headers: { ...(options.headers || {}) } };
  if (options.body !== undefined) {
    init.body = typeof options.body === 'string' ? options.body : JSON.stringify(options.body);
    (init.headers as Record<string, string>)['Content-Type'] = 'application/json';
  }
  const response = await fetch(url, init);
  const text = await response.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!response.ok) {
    const message = (data && typeof data === 'object' && (data.error || data.message)) || response.statusText || 'Request failed';
    throw new ApiError(String(message), response.status, data);
  }
  return data as T;
}

export const apiGet = <T = any>(path: string) => apiRequest<T>(path);
export const apiPost = <T = any>(path: string, body?: unknown) => apiRequest<T>(path, { method: 'POST', body });
export const apiPut = <T = any>(path: string, body?: unknown) => apiRequest<T>(path, { method: 'PUT', body });
export const apiPatch = <T = any>(path: string, body?: unknown) => apiRequest<T>(path, { method: 'PATCH', body });
export const apiDelete = <T = any>(path: string) => apiRequest<T>(path, { method: 'DELETE' });

// Axios-shaped default export ({ data }) for code written against the previous client.
const apiClient = {
  get: async <T = any>(path: string) => ({ data: await apiGet<T>(path) }),
  post: async <T = any>(path: string, body?: unknown) => ({ data: await apiPost<T>(path, body) }),
  put: async <T = any>(path: string, body?: unknown) => ({ data: await apiPut<T>(path, body) }),
  patch: async <T = any>(path: string, body?: unknown) => ({ data: await apiPatch<T>(path, body) }),
  delete: async <T = any>(path: string) => ({ data: await apiDelete<T>(path) }),
};

export default apiClient;
