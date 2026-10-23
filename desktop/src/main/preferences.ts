import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { normalizeBaseUrl } from './api-client';
import type { Preferences } from './routes';

export interface WindowBounds { width: number; height: number; x?: number; y?: number }
export interface StoredPreferences extends Preferences { bounds: WindowBounds }
const DEFAULT: StoredPreferences = { baseUrl: 'http://127.0.0.1:8000', repositoryId: null, bounds: { width: 1280, height: 900 } };

function bounded(value: unknown): WindowBounds {
  if (!value || typeof value !== 'object') return { ...DEFAULT.bounds };
  const input = value as Record<string, unknown>;
  const integer = (key: string, min: number, max: number) => typeof input[key] === 'number'
    && Number.isSafeInteger(input[key]) && input[key] >= min && input[key] <= max;
  return { width: integer('width', 768, 10000) ? input.width as number : DEFAULT.bounds.width,
    height: integer('height', 600, 10000) ? input.height as number : DEFAULT.bounds.height,
    ...(integer('x', -50000, 50000) ? { x: input.x as number } : {}),
    ...(integer('y', -50000, 50000) ? { y: input.y as number } : {}) };
}

export class PreferenceStore {
  private value: StoredPreferences = { ...DEFAULT, bounds: { ...DEFAULT.bounds } };
  private writing: Promise<void> = Promise.resolve();
  constructor(private readonly directory: string) {}
  async load(overrideUrl?: string): Promise<StoredPreferences> {
    try {
      const bytes = await readFile(join(this.directory, 'preferences.json'));
      if (bytes.length <= 4096) {
        const data = JSON.parse(bytes.toString('utf8')) as Record<string, unknown>;
        this.value = { baseUrl: normalizeBaseUrl(data.baseUrl), bounds: bounded(data.bounds),
          repositoryId: typeof data.repositoryId === 'string' && /^[0-9a-f-]{36}$/.test(data.repositoryId) ? data.repositoryId : null };
      }
    } catch { /* 首次启动或损坏偏好使用默认值，不读取其他开发机配置。 */ }
    if (overrideUrl !== undefined) {
      const baseUrl = normalizeBaseUrl(overrideUrl);
      if (baseUrl !== this.value.baseUrl) this.value = { ...this.value, baseUrl, repositoryId: null };
    }
    return this.snapshot();
  }
  snapshot(): StoredPreferences { return { ...this.value, bounds: { ...this.value.bounds } }; }
  update(next: Partial<StoredPreferences>): Promise<void> {
    this.value = { ...this.value, ...next, bounds: next.bounds ? bounded(next.bounds) : this.value.bounds };
    const content = JSON.stringify({ schema_version: 1, ...this.value });
    const write = this.writing.catch(() => {}).then(async () => {
      await mkdir(this.directory, { recursive: true });
      const temporary = join(this.directory, 'preferences.json.tmp');
      await writeFile(temporary, content, { encoding: 'utf8', mode: 0o600 });
      await rename(temporary, join(this.directory, 'preferences.json'));
    });
    this.writing = write;
    return write;
  }
}
