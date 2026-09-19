/** 与 `backend/main.py` 的 `/api/search` 契约对应的类型（见 tests/test_api.py）。 */

export type Clip = {
  season: number
  episode: number
  cue_index: number
  start_ms: number
  end_ms: number
  zh: string
  en: string
  /** 形如 `/v/S01E01.mp4`，供前端拼 Media Fragment */
  video: string
}

export type SearchResponse = {
  query: string
  count: number
  results: Clip[]
}

export async function searchClips(q: string, limit = 30): Promise<SearchResponse> {
  const params = new URLSearchParams({ q, limit: String(limit) })
  const response = await fetch(`/api/search?${params}`)
  if (!response.ok) {
    throw new Error(`搜索失败：HTTP ${response.status}`)
  }
  return (await response.json()) as SearchResponse
}

/** 剧集编号：`S01E01` */
export function episodeCode(clip: Clip): string {
  return `S${String(clip.season).padStart(2, '0')}E${String(clip.episode).padStart(2, '0')}`
}

/** 时间码：1:14:22（不足一小时则 2:03） */
export function formatTimecode(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const mm = String(minutes).padStart(hours > 0 ? 2 : 1, '0')
  const ss = String(seconds).padStart(2, '0')
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`
}

/**
 * Media Fragment URI：`/v/S01E01.mp4#t=2.38,4.84`。
 *
 * 起点精确到毫秒、end 由浏览器原生遵守（播到 4.84s 自动暂停），
 * 因此前端**无需**自写暂停逻辑。已实测，见 AGENTS.md 第 4 节。
 */
export function fragmentSrc(clip: Clip): string {
  return `${clip.video}#t=${clip.start_ms / 1000},${clip.end_ms / 1000}`
}

// ---------------------------------------------------------------------------
// 分享链接（M5）
// ---------------------------------------------------------------------------
export type ShareCreated = {
  token: string
  path: string
  clip: Clip
}

/**
 * 为一条台词创建分享链接。
 *
 * 只提交 `(季, 集, cue)` 三元组——**不提交台词与时间码**，内容由服务端回查，
 * 免得分享内容能被伪造成任意文本。
 */
export async function createShare(clip: Clip): Promise<ShareCreated> {
  const response = await fetch('/api/share', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      season: clip.season,
      episode: clip.episode,
      cue_index: clip.cue_index,
    }),
  })
  if (!response.ok) {
    throw new Error(`创建分享失败：HTTP ${response.status}`)
  }
  return (await response.json()) as ShareCreated
}

/** 按 token 取回分享的片段。 */
export async function fetchShare(token: string): Promise<Clip> {
  const response = await fetch(`/api/share/${encodeURIComponent(token)}`)
  if (response.status === 404) {
    throw new Error('分享链接无效或已失效')
  }
  if (!response.ok) {
    throw new Error(`加载分享失败：HTTP ${response.status}`)
  }
  return (await response.json()) as Clip
}

/** 从 `/s/{token}` 解析分享 token；不是分享页则返回 null。 */
export function shareTokenFromPath(pathname: string): string | null {
  const match = /^\/s\/([A-Za-z0-9_-]+)\/?$/.exec(pathname)
  return match ? match[1] : null
}
