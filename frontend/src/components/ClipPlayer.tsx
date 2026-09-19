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
 *
 * 末尾行为：播到 `end_ms` 自动暂停；**再点播放 = 重播这一句**（见 `restartIfFinished`）。
 */
const SEEK_TOLERANCE_S = 0.2
/** 判定「已经在末尾」的容差：`timeupdate` 粒度约 250ms，精确等于 endSec 不可靠 */
const END_TOLERANCE_S = 0.25

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
    // 本段是否已经「在末尾停过一次」。
    // ⚠️ 没这个标志就会踩坑：`stopAtEnd` 每次 timeupdate 都跑，于是播完后再点播放，
    // 视频从末尾继续 → 下一次 timeupdate 立刻又 pause（用户看到的是「播一下就停」）。
    let stoppedAtEnd = false

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
      if (video.currentTime < endSec) {
        // 离开末尾区间（例如把进度条拖回前面）→ 允许下次再停一次
        stoppedAtEnd = false
        return
      }
      if (stoppedAtEnd) return
      stoppedAtEnd = true
      video.pause()
    }

    /**
     * 播完（或拖到末尾）之后再点播放 → **重播这一句**。
     *
     * 这是「学台词」场景里最自然的行为：用户点播放是想再听一遍这句，
     * 而不是从这个片段的末尾继续往后跑。顺带也修掉了上面注释里那个「播一下就停」。
     */
    function restartIfFinished() {
      if (video === null) return
      if (video.currentTime >= endSec - END_TOLERANCE_S) {
        video.currentTime = startSec
        settled = true
        stoppedAtEnd = false
      }
    }

    // 多个时机重试：不同浏览器就绪的节点不同
    video.addEventListener('loadedmetadata', ensurePosition)
    video.addEventListener('loadeddata', ensurePosition)
    video.addEventListener('canplay', ensurePosition)
    video.addEventListener('timeupdate', ensurePosition)
    // 挂监听时可能已经就绪了
    ensurePosition()

    video.addEventListener('play', restartIfFinished)
    video.addEventListener('timeupdate', stopAtEnd)

    return () => {
      video.removeEventListener('loadedmetadata', ensurePosition)
      video.removeEventListener('loadeddata', ensurePosition)
      video.removeEventListener('canplay', ensurePosition)
      video.removeEventListener('timeupdate', ensurePosition)
      video.removeEventListener('play', restartIfFinished)
      video.removeEventListener('timeupdate', stopAtEnd)
    }
  }, [clip])

  if (clip === null) {
    return (
      <div className="flex aspect-video w-full items-center justify-center rounded-md border border-dashed border-line bg-panel px-6 text-center text-sm text-muted">
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
        className="aspect-video w-full rounded-md bg-black ring-1 ring-line"
      />

      <div className="rounded-md bg-panel p-4 ring-1 ring-line">
        <div className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-xs text-muted">
          <span className="rounded bg-brand px-1.5 py-0.5 font-medium text-white">
            {episodeCode(clip)}
          </span>
          <span>
            {formatTimecode(clip.start_ms)} – {formatTimecode(clip.end_ms)}
          </span>
          <span className="text-white/30">
            # {clip.cue_index} · {((clip.end_ms - clip.start_ms) / 1000).toFixed(1)}s
          </span>
        </div>

        <p className="text-base leading-relaxed text-white">{clip.zh}</p>
        <p className="mt-1.5 text-sm leading-relaxed text-muted italic">
          {clip.en}
        </p>
      </div>
    </div>
  )
}
