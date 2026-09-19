import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ClipList } from './components/ClipList'
import { ClipPlayer } from './components/ClipPlayer'
import { SharedClip } from './components/SharedClip'
import { createShare, searchClips, shareTokenFromPath, type Clip } from './api'

/** 滚动超过这么多像素后，显示「回到顶部」按钮 */
const BACK_TO_TOP_AFTER = 400

/**
 * 复制文本到剪贴板，尽力而为。
 *
 * 首选现代 Clipboard API —— 但它仅在**安全上下文**（HTTPS / localhost）可用。
 * 本地用 `http://192.168.x.x:5173` 调试时它是 undefined，所以必须兜底：
 * 退到已废弃的 `execCommand('copy')`，它在 http 下依然有效。
 */
async function copyText(text: string): Promise<boolean> {
  if (window.isSecureContext && navigator.clipboard !== undefined) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 继续走下面的兜底
    }
  }

  try {
    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.setAttribute('readonly', '')
    textarea.style.position = 'fixed'
    textarea.style.top = '-1000px'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    textarea.setSelectionRange(0, text.length) // iOS 需要这一步才真正选中
    const ok = document.execCommand('copy')
    document.body.removeChild(textarea)
    return ok
  } catch {
    return false
  }
}

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
  const [shareLink, setShareLink] = useState<string | null>(null)
  const [showBackToTop, setShowBackToTop] = useState(false)

  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    function onScroll() {
      setShowBackToTop(window.scrollY > BACK_TO_TOP_AFTER)
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  function handleBackToTop() {
    window.scrollTo({ top: 0, behavior: 'smooth' })
    // preventScroll：别让聚焦把平滑滚动打断
    inputRef.current?.focus({ preventScroll: true })
  }

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
    setShareLink(null)
    try {
      const { path } = await createShare(active)
      const link = `${window.location.origin}${path}`

      if (await copyText(link)) {
        setShareNote('链接已复制到剪贴板')
      } else {
        // 复制不成功时给一个能一键全选的输入框，而不是让用户去拖选长 URL
        setShareLink(link)
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
        <div className="mx-auto max-w-6xl px-4 py-4 sm:py-5">
          <h1 className="text-lg font-semibold">
            生活大爆炸
            <span className="ml-2 font-normal text-slate-400">台词搜索</span>
          </h1>
          <p className="mt-1 text-xs text-slate-500">
            12 季 · 279 集 · 117,842 条台词 —— 搜到即可精确播放对应片段
          </p>

          <form onSubmit={handleSubmit} className="mt-4 flex gap-2">
            <input
              ref={inputRef}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="输入关键词，如 photon / 狭缝 / 谢尔顿"
              enterKeyHint="search"
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
              /* text-base(16px)：低于 16px 时 iOS Safari 聚焦会强制放大页面 */
              className="min-w-0 flex-1 rounded-lg border border-slate-300 px-3 py-2.5 text-base outline-none transition focus:border-sky-400 focus:ring-2 focus:ring-sky-100"
            />
            <button
              type="submit"
              disabled={loading || query.trim() === ''}
              className="shrink-0 cursor-pointer rounded-lg bg-sky-600 px-5 py-2.5 text-base font-medium text-white transition hover:bg-sky-700 active:bg-sky-800 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {loading ? '搜索中…' : '搜索'}
            </button>
          </form>
        </div>
      </header>

      {/*
        布局策略：
        - 手机：播放器 section 排在前面（order-1）并 `sticky top-0` 吸顶。
        - 桌面（lg+）：切回两栏 grid，播放器在右栏吸顶。

        ⚠️ sticky 必须加在 **section** 上，不能加在 <video> 上。
        sticky 的粘滞范围受「直接父容器」限制：<video> 的父容器只有「视频 + 台词卡」
        那点高度，滚过去就脱离粘滞（实测：滚动 1200px 后 top = -902，即完全失败）。
        section 的父容器是整个 <main>（播放器 + 长列表），才能一路粘住（实测 top = 0）。
      */}
      <main className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-4 lg:grid lg:grid-cols-[1fr_1.15fr] lg:items-start lg:gap-6 lg:py-6">
        <section className="sticky top-0 z-10 order-1 min-w-0 bg-slate-50 pt-1 pb-3 lg:order-2 lg:top-6 lg:bg-transparent lg:pt-0 lg:pb-0">
          <ClipPlayer clip={active} />

          {active !== null && (
            <div className="mt-3">
              <button
                type="button"
                onClick={handleShare}
                disabled={sharing}
                className="w-full cursor-pointer rounded-lg border border-sky-300 bg-white px-4 py-2.5 text-sm font-medium text-sky-700 transition hover:bg-sky-50 active:bg-sky-100 disabled:cursor-not-allowed disabled:text-slate-400"
              >
                {sharing ? '生成分享链接…' : '分享这条台词'}
              </button>

              {shareNote !== null && (
                <p className="mt-2 rounded-lg bg-slate-100 px-3 py-2 text-xs text-slate-600">
                  {shareNote}
                </p>
              )}

              {shareLink !== null && (
                <div className="mt-2">
                  <p className="mb-1 text-xs text-slate-500">
                    自动复制被浏览器拦下了，点一下全选再复制：
                  </p>
                  <input
                    readOnly
                    value={shareLink}
                    onFocus={(event) => event.currentTarget.select()}
                    onClick={(event) => event.currentTarget.select()}
                    className="w-full rounded-lg border border-slate-300 bg-slate-50 px-3 py-2 text-xs break-all text-slate-700"
                  />
                </div>
              )}
            </div>
          )}
        </section>

        <section className="order-2 min-w-0 lg:order-1">
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
            <p className="rounded-xl border border-dashed border-slate-300 bg-white p-6 text-center text-sm text-slate-400">
              输入关键词开始搜索。
              <br className="sm:hidden" />
              中文两字词（如「狭缝」）也能搜到。
            </p>
          )}

          {error === null && searched && results.length === 0 && (
            <p className="rounded-xl border border-dashed border-slate-300 bg-white p-6 text-center text-sm text-slate-400">
              没有找到包含「{keyword}」的台词
            </p>
          )}

          {results.length > 0 && (
            /* 只有大屏才让列表独立滚动；手机上交还给页面，避免「嵌套滚动」 */
            <div className="lg:max-h-[70vh] lg:overflow-y-auto lg:pr-1">
              <ClipList
                clips={results}
                active={active}
                keyword={keyword}
                onSelect={setActive}
              />
            </div>
          )}
        </section>
      </main>

      {/* 滑远之后一键回到顶部，并把焦点落到搜索框——省掉「再点一下输入框」 */}
      {showBackToTop && (
        <button
          type="button"
          onClick={handleBackToTop}
          aria-label="回到顶部并聚焦搜索框"
          className="fixed right-4 bottom-[calc(1.5rem+env(safe-area-inset-bottom))] z-20 flex h-11 w-11 cursor-pointer items-center justify-center rounded-full bg-slate-800/85 text-white shadow-lg backdrop-blur transition hover:bg-slate-800 active:scale-95"
        >
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M12 19V5M5 12l7-7 7 7" />
          </svg>
        </button>
      )}
    </div>
  )
}
