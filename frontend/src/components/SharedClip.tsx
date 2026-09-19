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
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto max-w-3xl px-4 py-5">
          <h1 className="text-lg font-semibold">
            生活大爆炸
            <span className="ml-2 font-normal text-slate-400">分享的片段</span>
          </h1>
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-4 py-6">
        {error !== null && (
          <p className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            {error}
          </p>
        )}

        {error === null && clip === null && (
          <p className="rounded-lg border border-dashed border-slate-300 bg-white p-6 text-center text-sm text-slate-400">
            加载中…
          </p>
        )}

        {clip !== null && (
          <>
            <ClipPlayer clip={clip} />
            <p className="mt-6 text-center text-sm">
              <a href="/" className="text-sky-600 hover:underline">
                去搜索更多台词 →
              </a>
            </p>
          </>
        )}
      </main>
    </div>
  )
}
