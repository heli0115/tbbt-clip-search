import { useState, type FormEvent } from 'react'
import { ClipList } from './components/ClipList'
import { ClipPlayer } from './components/ClipPlayer'
import { SharedClip } from './components/SharedClip'
import { createShare, searchClips, shareTokenFromPath, type Clip } from './api'

export default function App() {
  // `/s/{token}` 是分享页；其余情况是搜索页
  const shareToken = shareTokenFromPath(window.location.pathname)

  const [query, setQuery] = useState('')
  const [keyword, setKeyword] = useState('')
  const [results, setResults] = useState<Clip[]>([])
  const [active, setActive] = useState<Clip | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [searched, setSearched] = useState(false)
  const [sharing, setSharing] = useState(false)
  const [shareNote, setShareNote] = useState<string | null>(null)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    const q = query.trim()
    if (!q) return

    setLoading(true)
    setError(null)
    setShareNote(null)
    try {
      const data = await searchClips(q)
      setResults(data.results)
      setActive(data.results[0] ?? null)
      setKeyword(q)
      setSearched(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setResults([])
      setActive(null)
    } finally {
      setLoading(false)
    }
  }

  async function handleShare() {
    if (active === null) return

    setSharing(true)
    setShareNote(null)
    try {
      const { path } = await createShare(active)
      const url = `${window.location.origin}${path}`
      try {
        await navigator.clipboard.writeText(url)
        setShareNote(`链接已复制到剪贴板：${url}`)
      } catch {
        // 非 HTTPS 或用户拒绝授权时，退化为手动复制
        setShareNote(`复制失败，请手动复制：${url}`)
      }
    } catch (err) {
      setShareNote(err instanceof Error ? err.message : String(err))
    } finally {
      setSharing(false)
    }
  }

  if (shareToken !== null) {
    return <SharedClip token={shareToken} />
  }

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto max-w-6xl px-4 py-5">
          <h1 className="text-lg font-semibold">
            生活大爆炸
            <span className="ml-2 font-normal text-slate-400">台词搜索</span>
          </h1>
          <p className="mt-1 text-xs text-slate-500">
            12 季 · 279 集 · 117,842 条台词 —— 搜到即可精确播放对应片段
          </p>

          <form onSubmit={handleSubmit} className="mt-4 flex gap-2">
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="输入关键词，如 photon / 狭缝 / 谢尔顿"
              className="flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none transition focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
            />
            <button
              type="submit"
              disabled={loading || query.trim() === ''}
              className="cursor-pointer rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-sky-700 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {loading ? '搜索中…' : '搜索'}
            </button>
          </form>
        </div>
      </header>

      <main className="mx-auto grid max-w-6xl items-start gap-6 px-4 py-6 lg:grid-cols-[1fr_1.15fr]">
        <section>
          <h2 className="mb-3 text-sm font-medium text-slate-600">
            搜索结果
            {searched && !loading && (
              <span className="ml-2 font-normal text-slate-400">
                {results.length} 条
              </span>
            )}
          </h2>

          {error !== null && (
            <p className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
              {error}
            </p>
          )}

          {error === null && !searched && (
            <p className="rounded-lg border border-dashed border-slate-300 bg-white p-6 text-center text-sm text-slate-400">
              输入关键词开始搜索。中文两字词（如「狭缝」）也能搜到。
            </p>
          )}

          {error === null && searched && results.length === 0 && (
            <p className="rounded-lg border border-dashed border-slate-300 bg-white p-6 text-center text-sm text-slate-400">
              没有找到包含「{keyword}」的台词
            </p>
          )}

          {results.length > 0 && (
            <div className="max-h-[70vh] overflow-y-auto pr-1">
              <ClipList
                clips={results}
                active={active}
                keyword={keyword}
                onSelect={setActive}
              />
            </div>
          )}
        </section>

        <section className="lg:sticky lg:top-6">
          <ClipPlayer clip={active} />

          {active !== null && (
            <div className="mt-3">
              <button
                type="button"
                onClick={handleShare}
                disabled={sharing}
                className="w-full cursor-pointer rounded-lg border border-sky-300 bg-white px-4 py-2 text-sm font-medium text-sky-700 transition hover:bg-sky-50 disabled:cursor-not-allowed disabled:text-slate-400"
              >
                {sharing ? '生成分享链接…' : '分享这条台词'}
              </button>

              {shareNote !== null && (
                <p className="mt-2 break-all rounded-lg bg-slate-100 px-3 py-2 text-xs text-slate-600">
                  {shareNote}
                </p>
              )}
            </div>
          )}
        </section>
      </main>
    </div>
  )
}
