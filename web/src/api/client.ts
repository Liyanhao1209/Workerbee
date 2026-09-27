/**
 * 内核 HTTP 客户端。
 *
 * 三条纪律：
 * 1. **连接失败不是空数据。** 内核没起来与「列表为空」是两件不同的事，
 *    界面必须显示「无法连接内核」而不是空白页。因此网络层错误单独成类。
 * 2. **状态码是契约的一部分。** 409（编辑冲突）、422（提交诊断）、401（令牌）
 *    都带结构化响应体，不能被压成一句字符串丢掉。
 * 3. 令牌只从 localStorage 读、只放进 X-Workerbee-Token 头；不回显、不打日志。
 */

import type { ConflictResponse, Diagnostic, ValidationReport } from './types';
import { isValidationReport } from './guards';

export const TOKEN_STORAGE_KEY = 'workerbee.token';

export function readToken(): string {
  try {
    return localStorage.getItem(TOKEN_STORAGE_KEY) ?? '';
  } catch {
    return '';
  }
}

export function writeToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_STORAGE_KEY, token);
    else localStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    /* 隐私模式下 localStorage 不可写：本次会话仍可用，只是不持久。 */
  }
}

export type ApiErrorKind =
  | 'unreachable'
  /** 401：令牌缺失或无效。 */
  | 'unauthorized'
  /** 409：编辑冲突，带最新版本供 diff。 */
  | 'conflict'
  /** 422：请求合法但内容不可执行，带可定位的 diagnostics。 */
  | 'unprocessable'
  | 'not_found'
  | 'http'
  | 'parse';

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number;
  readonly detail: string;
  readonly hint: string | null;
  readonly body: unknown;

  constructor(init: {
    kind: ApiErrorKind;
    status: number;
    detail: string;
    hint?: string | null;
    body?: unknown;
  }) {
    super(init.detail);
    this.name = 'ApiError';
    this.kind = init.kind;
    this.status = init.status;
    this.detail = init.detail;
    this.hint = init.hint ?? null;
    this.body = init.body ?? null;
  }

  /** 内核不可达——这是「无法连接内核」，不是「没有数据」。 */
  get unreachable(): boolean {
    return this.kind === 'unreachable';
  }

  /** 编辑冲突（D-02）。返回最新修订供用户比较。 */
  asConflict(): ConflictResponse | null {
    if (this.kind !== 'conflict') return null;
    const body = this.body;
    if (typeof body !== 'object' || body === null) return null;
    const b = body as Partial<ConflictResponse>;
    if (typeof b.workflow_id !== 'string') return null;
    return {
      detail: typeof b.detail === 'string' ? b.detail : this.detail,
      workflow_id: b.workflow_id,
      latest_revision_seq: typeof b.latest_revision_seq === 'number' ? b.latest_revision_seq : 0,
      latest_revision: b.latest_revision ?? null,
      hint: b.hint ?? null,
    };
  }

  /** 提交/校验失败时可定位的诊断列表（RUN-01、WF-05）。 */
  asDiagnostics(): Diagnostic[] {
    const report = this.asValidationReport();
    return report?.diagnostics ?? [];
  }

  asValidationReport(): ValidationReport | null {
    const body = this.body;
    if (typeof body !== 'object' || body === null) return null;
    const b = body as Record<string, unknown>;
    // 422 的两种可能形态：直接的 ValidationReport，或 {detail, report}
    if (isValidationReport(b)) return b;
    const nested = b['report'];
    if (isValidationReport(nested)) return nested;
    const diagnostics = b['diagnostics'];
    if (Array.isArray(diagnostics)) {
      return { mode: 'launch', diagnostics: diagnostics as Diagnostic[] };
    }
    return null;
  }
}

function kindForStatus(status: number): ApiErrorKind {
  if (status === 401) return 'unauthorized';
  if (status === 404) return 'not_found';
  if (status === 409) return 'conflict';
  if (status === 422) return 'unprocessable';
  return 'http';
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  /** 查询参数。值为 undefined / null 的键会被跳过。 */
  query?: Record<string, string | number | boolean | undefined | null>;
  signal?: AbortSignal;
  /** 免鉴权端点（/api/health）用 true。 */
  noAuth?: boolean;
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

async function readBody(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

function describeBody(body: unknown, fallback: string): { detail: string; hint: string | null } {
  if (typeof body === 'string' && body.trim()) return { detail: body.slice(0, 500), hint: null };
  if (typeof body === 'object' && body !== null) {
    const b = body as Record<string, unknown>;
    const detail = typeof b['detail'] === 'string' ? b['detail'] : fallback;
    let hint: string | null = typeof b['hint'] === 'string' ? b['hint'] : null;
    if (!hint && Array.isArray(b['diagnostics'])) {
      const first = b['diagnostics'][0] as { message?: unknown } | undefined;
      if (first && typeof first.message === 'string') hint = first.message;
    }
    if (!hint && b['report'] && typeof b['report'] === 'object') {
      const diags = (b['report'] as { diagnostics?: unknown }).diagnostics;
      if (Array.isArray(diags) && diags.length > 0) {
        const first = diags[0] as { message?: unknown };
        if (typeof first.message === 'string') hint = first.message;
      }
    }
    return { detail, hint };
  }
  return { detail: fallback, hint: null };
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  if (!options.noAuth) {
    const token = readToken();
    if (token) headers['X-Workerbee-Token'] = token;
  }

  let res: Response;
  try {
    res = await fetch(buildUrl(path, options.query), {
      method: options.method ?? 'GET',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
      credentials: 'same-origin',
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new ApiError({
      kind: 'unreachable',
      status: 0,
      detail: err instanceof Error ? err.message : String(err),
      hint: '内核可能未启动。请确认 workerbee-core 已运行并可访问。',
    });
  }

  const body = await readBody(res);

  if (!res.ok) {
    const { detail, hint } = describeBody(body, `${res.status} ${res.statusText}`);
    throw new ApiError({ kind: kindForStatus(res.status), status: res.status, detail, hint, body });
  }

  return body as T;
}

/** 无副作用的一次性连通性探测。免鉴权。 */
export async function pingHealth(signal?: AbortSignal): Promise<{ ok: boolean; version: string }> {
  return await request<{ ok: boolean; version: string }>('/api/health', { noAuth: true, signal });
}
