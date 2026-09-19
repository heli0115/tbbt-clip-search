import { useEffect, useState } from 'react'
import { fetchShare, type Clip } from '../api'
import { ClipPlayer } from './ClipPlayer'

/**
 * 分享页：`/s/{token}`。
 *
 * token 由后端 `secrets.token_urlsafe` 生成、存在 shares 表里（AGENTS.md 第 7 节），
 * 所以链接不可枚举、不被爬虫索引。页面只做一件事：拿到片段并播放它。
 */
export function SharedClip({ token }: { token: string }) {
  const [clip, setClip] = useState<Clip | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    fetchShare(token)
      .then((data) => {
        if (alive) setClip(data)
      })
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      alive = false
    }
  }, [token])

  return (
    <div className="min-h-screen bg-ink text-white">
      <header className="border-b border-line bg-gradient-to-b from-black to-ink">
        <div className="mx-auto max-w-3xl px-4 py-5">
          <h1 className="flex items-baseline gap-3">
            <span className="text-2xl leading-none font-black tracking-tighter text-brand sm:text-3xl">
              TBBT
            </span>
            <span className="text-sm font-medium text-white/85">
              生活大爆炸 · 分享的片段
            </span>
          </h1>
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-4 py-6">
        {error !== null && (
          <p className="rounded-md border border-brand/40 bg-brand/10 p-4 text-sm text-white">
            {error}
          </p>
        )}

        {error === null && clip === null && (
          <p className="rounded-md border border-dashed border-line bg-panel/60 p-6 text-center text-sm text-muted">
            加载中…
          </p>
        )}

        {clip !== null && (
          <>
            <ClipPlayer clip={clip} />
            <p className="mt-6 text-center text-sm">
              <a
                href="/"
                className="font-medium text-brand hover:text-brand-hover hover:underline"
              >
                去搜索更多台词 →
              </a>
            </p>
          </>
        )}
      </main>
    </div>
  )
}
