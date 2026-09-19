import { useEffect, useLayoutEffect, useRef } from 'react'
import type { Clip } from '../api'
import { episodeCode, formatTimecode } from '../api'

/**
 * 片段播放器。
 *
 * 定位**完全由 JS 控制，不再用 Media Fragment（`#t=a,b`）**。
 *
 * 为什么放弃它：Media Fragment 在移动端支持不可靠（Android 上时灵时不灵），
 * 而且浏览器原生 seek 会和 JS 设的 `currentTime` 互相打架 —— 结果就是
 * 「有的从头播、有的从台词播」这种飘忽行为。只保留一套机制，行为才确定。
 *
 * 另外不能只在 `loadedmetadata` 里 seek：那一刻 `seekable` 范围常常还没就绪
 * （Android 尤其明显），设置 `currentTime` 会被静默忽略。所以在多个时机重试，
 * 并以「已经落在起点之后」作为收敛条件。
 */
const SEEK_TOLERANCE_S = 0.2

export function ClipPlayer({ clip }: { clip: Clip | null }) {
  const videoRef = useRef<HTMLVideoElement>(null)

  /*
    为什么显式 play() 而不是靠 autoPlay 属性：

    移动浏览器对**有声**视频的自动播放管得很严。`autoPlay` 要等视频加载到可播放
    时才触发，那时距用户点击已经过了一段时间，浏览器的「用户手势」授权窗口已过期，
    于是被静默拦下。useLayoutEffect 在同一个 task 里同步执行（紧跟点击后的那次提交），
    授权仍然有效，play() 的成功率明显更高。失败也不报错，退化为用户手点播放键。
  */
  useLayoutEffect(() => {
    const video = videoRef.current
    if (video === null || clip === null) return

    const result = video.play()
    if (result !== undefined) {
      result.catch(() => {
        // 被自动播放策略拦下：保持暂停，等用户点播放键
      })
    }
  }, [clip])

  useEffect(() => {
    const video = videoRef.current
    if (video === null || clip === null) return

    const startSec = clip.start_ms / 1000
    const endSec = clip.end_ms / 1000

    // 已经落在起点（或之后）就收工，避免和用户手动拖动打架
    let settled = false

    function ensurePosition() {
      if (video === null || settled || video.readyState < 1) return
      if (video.currentTime >= startSec - SEEK_TOLERANCE_S) {
        settled = true
        return
      }
      video.currentTime = startSec
    }

    function stopAtEnd() {
      if (video === null) return
      if (video.currentTime >= endSec) {
        video.pause()
      }
    }

    // 多个时机重试：不同浏览器就绪的节点不同
    video.addEventListener('loadedmetadata', ensurePosition)
    video.addEventListener('loadeddata', ensurePosition)
    video.addEventListener('canplay', ensurePosition)
    video.addEventListener('timeupdate', ensurePosition)
    // 挂监听时可能已经就绪了
    ensurePosition()

    video.addEventListener('timeupdate', stopAtEnd)

    return () => {
      video.removeEventListener('loadedmetadata', ensurePosition)
      video.removeEventListener('loadeddata', ensurePosition)
      video.removeEventListener('canplay', ensurePosition)
      video.removeEventListener('timeupdate', ensurePosition)
      video.removeEventListener('timeupdate', stopAtEnd)
    }
  }, [clip])

  if (clip === null) {
    return (
      <div className="flex aspect-video w-full items-center justify-center rounded-xl border border-dashed border-slate-300 bg-white px-6 text-center text-sm text-slate-400">
        选择一条台词，这里会播放对应片段
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      {/*
        playsInline 是 iOS 的硬性要求：不加的话 Safari 一按播放就强制全屏。
        src 用**不带 fragment** 的裸地址——定位交给上面的 JS。
        key 保证换片段时重建 <video>，从干净状态重新加载。
      */}
      <video
        key={clip.video}
        ref={videoRef}
        src={clip.video}
        controls
        playsInline
        preload="metadata"
        className="aspect-video w-full rounded-xl bg-black shadow-sm"
      />

      <div className="rounded-xl border border-slate-200 bg-white p-4">
        <div className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-xs text-slate-500">
          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-700">
            {episodeCode(clip)}
          </span>
          <span>
            {formatTimecode(clip.start_ms)} – {formatTimecode(clip.end_ms)}
          </span>
          <span className="text-slate-300">
            # {clip.cue_index} · {((clip.end_ms - clip.start_ms) / 1000).toFixed(1)}s
          </span>
        </div>

        <p className="text-base leading-relaxed text-slate-900">{clip.zh}</p>
        <p className="mt-1.5 text-sm leading-relaxed text-slate-500 italic">
          {clip.en}
        </p>
      </div>
    </div>
  )
}
